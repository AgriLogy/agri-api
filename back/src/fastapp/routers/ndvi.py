"""NDVI per sector from Sentinel-2 via Copernicus (agri-api#459, phase 2).

All under ``/farm-map/`` (already routed to fastapp by nginx):

- ``GET /farm-map/ndvi/config``                    → is it configured + legend
- ``GET /farm-map/ndvi/sectors/{pk}/dates``        → acquisition dates
- ``GET /farm-map/ndvi/sectors/{pk}/image?date=``  → PNG clipped to the sector
- ``GET /farm-map/ndvi/sectors/{pk}/stats``        → mean/min/max per date

Only the sector's owner can read it. NDVI is vegetation vigour from 10 m
pixels: it is not a crop-density measurement and does not show where
irrigation water went; the front says so next to every number.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from agri.core.database import session_scope
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select

from fastapp.auth import AuthedUser, get_current_user
from fastapp.farm_map_tables import sector_meta
from fastapp.json import DjangoStyleJSONResponse
from fastapp.satellite.cdse import (
    IMAGE_KINDS,
    INDEXES,
    NDVI_LEGEND,
    CDSEClient,
    SatelliteError,
    legend,
)
from fastapp.schema_compat import sector_geometry_available
from fastapp.settings import get_settings

router = APIRouter(tags=["farm-map"])

_STATUS = {
    "not_configured": 503,
    "auth": 502,
    "unavailable": 502,
    "rate_limit": 429,
    "bad_request": 422,
}
_client: CDSEClient | None = None
_client_key: tuple[str, str] | None = None


def get_client() -> CDSEClient:
    """Process-wide client (keeps the OAuth token between requests)."""
    global _client, _client_key
    s = get_settings()
    key = (s.cdse_client_id, s.cdse_client_secret)
    if _client is None or _client_key != key:
        _client = CDSEClient(*key, cache_url=s.celery_broker_url)
        _client_key = key
    return _client


def _err(exc: SatelliteError) -> DjangoStyleJSONResponse:
    return DjangoStyleJSONResponse(
        {"detail": str(exc), "code": exc.kind}, status_code=_STATUS[exc.kind]
    )


def _sector_geometry(pk: int, user_id: int) -> dict[str, Any] | None:
    with session_scope() as session:
        if not sector_geometry_available(session):
            return None
        row = session.execute(
            select(sector_meta.c.geometry).where(
                sector_meta.c.id == pk, sector_meta.c.user_id == user_id
            )
        ).first()
    return row[0] if row is not None and isinstance(row[0], dict) else None


def _range(days: int) -> tuple[dt.date, dt.date]:
    end = dt.datetime.now(dt.UTC).date()
    return end - dt.timedelta(days=days), end


_NO_SECTOR = DjangoStyleJSONResponse(
    {"detail": "Sector not found or not drawn yet.", "code": "no_sector"},
    status_code=404,
)


@router.get("/farm-map/ndvi/config", summary="Whether NDVI is available + legend")
def ndvi_config(user: AuthedUser = Depends(get_current_user)):
    s = get_settings()
    return {
        "configured": bool(s.cdse_client_id and s.cdse_client_secret),
        "source": "Copernicus Sentinel-2 L2A (10 m)",
        "legend": NDVI_LEGEND,
        "indexes": [
            {
                "id": k,
                "use": v["use"],
                "legend": legend(k),
                "classes": {"low_below": v["classes"][0], "high_from": v["classes"][1]},
            }
            for k, v in INDEXES.items()
        ],
        "image_kinds": list(IMAGE_KINDS),
    }


@router.get(
    "/farm-map/ndvi/sectors/{pk}/dates", summary="Sentinel-2 dates over a sector"
)
def ndvi_dates(
    pk: int,
    days: int = Query(120, ge=7, le=730),
    max_cloud: int = Query(40, ge=0, le=100),
    user: AuthedUser = Depends(get_current_user),
):
    geometry = _sector_geometry(pk, user.id)
    if geometry is None:
        return _NO_SECTOR
    start, end = _range(days)
    try:
        dates = get_client().acquisitions(geometry, start, end, max_cloud)
    except SatelliteError as exc:
        return _err(exc)
    return {
        "sector_id": pk,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "dates": dates,
    }


@router.get("/farm-map/ndvi/sectors/{pk}/image", summary="NDVI PNG clipped to a sector")
def ndvi_image(
    pk: int,
    date: dt.date = Query(...),
    index: str = Query("ndvi", pattern="^(ndvi|ndmi|ndre|false_color)$"),
    user: AuthedUser = Depends(get_current_user),
):
    geometry = _sector_geometry(pk, user.id)
    if geometry is None:
        return _NO_SECTOR
    try:
        png = get_client().ndvi_png(geometry, date, index)
    except SatelliteError as exc:
        return _err(exc)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/farm-map/ndvi/sectors/{pk}/stats", summary="NDVI statistics per date")
def ndvi_stats(
    pk: int,
    days: int = Query(120, ge=7, le=730),
    index: str = Query("ndvi", pattern="^(ndvi|ndmi|ndre)$"),
    user: AuthedUser = Depends(get_current_user),
):
    geometry = _sector_geometry(pk, user.id)
    if geometry is None:
        return _NO_SECTOR
    start, end = _range(days)
    try:
        series = get_client().ndvi_stats(geometry, start, end, index)
    except SatelliteError as exc:
        return _err(exc)
    return {
        "sector_id": pk,
        "index": index,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "series": series,
    }
