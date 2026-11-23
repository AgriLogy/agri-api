"""Farm map, phase 3 (agri-api#459): decision layer, forecast layer,
sector shape history and KML import.

- ``GET  /farm-map/sectors/{pk}/insights?lang=``             water balance + advice + NDVI × soil
- ``GET  /farm-map/forecast?days=``                           rain / ET₀ per drawn sector
- ``GET  /sectors/{pk}/geometry-history``                     every shape the sector had
- ``POST /sectors/{pk}/geometry-history/{rev}/restore``       put an old shape back
- ``POST /farm-map/sectors/import-kml``                       KML/KMZ → sectors (dry run first)

Owner-scoped like the rest of the farm map; a technician with an active grant
reads the owner's sectors that hold a granted zone (``sectors.readable_sector``).
Writes need the editor level and are refused to technician logins.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from agri.core.database import session_scope
from agri.core.geometry import (
    GeometryError,
    area_hectares,
    perimeter_m,
    validate_polygon,
)
from agri.db.analytics import AnalyticsSector
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select

from fastapp.auth import AuthedUser, get_current_user, require_level
from fastapp.json import DjangoStyleJSONResponse
from fastapp.kml import MAX_KML_BYTES, KmlError, parse_kml
from fastapp.routers.sectors import (
    SectorIn,
    _apply_geometry,
    _block_if_technician,
    _owned,
    _persist_geometry,
    _serialize,
    readable_sector,
    visible_sector_ids,
)
from fastapp.routers.selfreads import read_scope_for
from fastapp.satellite import store as satellite_store
from fastapp.schema_compat import sector_geometry_available
from fastapp.sector_insights import resolve_lang, sector_insights, sectors_forecast

router = APIRouter(tags=["farm-map"])

_NOT_FOUND = {"detail": "Sector not found.", "code": "no_sector"}


@router.get(
    "/farm-map/sectors/{pk}/insights",
    summary="Water balance, irrigation advice and NDVI × soil reading for a sector",
)
def insights(
    pk: int,
    lang: str | None = Query(
        None,
        description="fr | ar | en. Defaults to the caller's preferred_language, "
        "else fr; an unknown value falls back to fr.",
    ),
    user: AuthedUser = Depends(get_current_user),
):
    language = resolve_lang(lang, user.preferred_language)
    with session_scope() as session:
        scope = read_scope_for(session, user.id)
        body = None
        if readable_sector(session, pk, scope) is not None:
            body = sector_insights(
                session, pk, scope.owner_id, zone_ids=scope.zone_ids, lang=language
            )
    if body is None:
        return DjangoStyleJSONResponse(_NOT_FOUND, status_code=404)
    return body


@router.get("/farm-map/forecast", summary="Rain and ET₀ forecast per drawn sector")
def forecast(
    days: int = Query(3, ge=1, le=7),
    user: AuthedUser = Depends(get_current_user),
):
    with session_scope() as session:
        scope = read_scope_for(session, user.id)
        return sectors_forecast(
            session,
            scope.owner_id,
            days=days,
            sector_ids=visible_sector_ids(session, scope),
        )


@router.get("/sectors/{pk}/geometry-history", summary="Shapes the sector has had")
def geometry_history(pk: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        if readable_sector(session, pk, read_scope_for(session, user.id)) is None:
            return DjangoStyleJSONResponse(_NOT_FOUND, status_code=404)
        return {"sector_id": pk, "revisions": satellite_store.revisions(session, pk)}


@router.post(
    "/sectors/{pk}/geometry-history/{revision_id}/restore",
    summary="Restore an earlier shape (recorded as a new revision)",
)
def restore_geometry(
    pk: int,
    revision_id: int,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    with session_scope(commit=True) as session:
        guard = _block_if_technician(session, user.id)
        if guard is not None:
            return guard
        sector = _owned(session, pk, user.id)
        if sector is None:
            return DjangoStyleJSONResponse(_NOT_FOUND, status_code=404)
        rev = satellite_store.revision(session, pk, revision_id)
        if rev is None:
            return DjangoStyleJSONResponse(
                {"detail": "Revision not found.", "code": "no_revision"},
                status_code=404,
            )
        payload = SectorIn(name=sector.name, geometry=rev.geometry)
        error = _apply_geometry(session, sector, payload)
        if error is not None:
            return error
        session.flush()
        _persist_geometry(session, sector, payload)
        satellite_store.record_revision(
            session,
            sector_id=sector.id,
            user_id=user.id,
            geometry=getattr(sector, "geometry", None),
            area_ha=getattr(sector, "area_ha", None),
            perimeter_m=getattr(sector, "perimeter_m", None),
            source="restore",
        )
        return _serialize(session, sector)


def _shape_summary(name: str, geometry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "area_ha": round(area_hectares(geometry), 4),
        "perimeter_m": round(perimeter_m(geometry), 1),
        "geometry": geometry,
        "id": None,
    }


@router.post(
    "/farm-map/sectors/import-kml", summary="Create sectors from a KML/KMZ file"
)
async def import_kml(
    file: UploadFile = File(...),
    dry_run: bool = Form(True),
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    data = await file.read(MAX_KML_BYTES + 1)
    try:
        parsed = parse_kml(data, file.filename or "")
    except KmlError as exc:
        return DjangoStyleJSONResponse({"detail": str(exc)}, status_code=400)

    with session_scope(commit=not dry_run) as session:
        guard = _block_if_technician(session, user.id)
        if guard is not None:
            return guard
        if not sector_geometry_available(session):
            return DjangoStyleJSONResponse(
                {"detail": "Sector shapes are not available on this server."},
                status_code=503,
            )
        taken = {
            n.strip().lower()
            for n in session.scalars(
                select(func.lower(AnalyticsSector.name)).where(
                    AnalyticsSector.user_id == user.id
                )
            )
        }
        sectors: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for shape in parsed.shapes:
            if shape.geometry is None:
                skipped.append(
                    {"name": shape.name, "reason": shape.reason or "invalid_geometry"}
                )
                continue
            key = shape.name.strip().lower()
            if key in taken:
                skipped.append({"name": shape.name, "reason": "duplicate_name"})
                continue
            try:
                geometry = validate_polygon(shape.geometry)
            except (GeometryError, ValueError, TypeError):
                skipped.append({"name": shape.name, "reason": "invalid_geometry"})
                continue
            taken.add(key)
            summary = _shape_summary(shape.name, geometry)
            if not dry_run:
                sector = AnalyticsSector(name=shape.name, user_id=user.id)
                payload = SectorIn(name=shape.name, geometry=geometry)
                error = _apply_geometry(session, sector, payload)
                if error is not None:
                    skipped.append({"name": shape.name, "reason": "invalid_geometry"})
                    continue
                session.add(sector)
                session.flush()
                _persist_geometry(session, sector, payload)
                satellite_store.record_revision(
                    session,
                    sector_id=sector.id,
                    user_id=user.id,
                    geometry=getattr(sector, "geometry", None),
                    area_ha=getattr(sector, "area_ha", None),
                    perimeter_m=getattr(sector, "perimeter_m", None),
                    source="kml",
                )
                summary["id"] = sector.id
            sectors.append(summary)

    return DjangoStyleJSONResponse(
        {
            "dry_run": dry_run,
            "imported_at": dt.datetime.now(dt.UTC).isoformat() if not dry_run else None,
            "sectors": sectors,
            "skipped": skipped,
        },
        status_code=201 if not dry_run and sectors else 200,
    )
