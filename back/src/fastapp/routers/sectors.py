"""fastapp /sectors — a user's organizational grouping of zones.

User → Sector → Zone (one implicit farm per user). Sectors are named buckets
with no geometry; a zone points at its sector via ``analytics_zone.sector_id``
(nullable = unassigned). Backs the farm-visualization page + the header's
sector picker.

All endpoints are owner-scoped (JWT ``user.id``); technicians (read-only) are
blocked from writes (403), matching the other user-owned routers. DB access is
SQLAlchemy via agri-core (AnalyticsSector / AnalyticsZone from agri-db 0.17.0).
"""

from __future__ import annotations

import logging
from typing import Any

from agri.core.database import session_scope
from agri.db.analytics import AnalyticsSector, AnalyticsZone
from agri.db.users import CustomUserCustomuser
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["sectors"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SectorIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class SectorZonesIn(BaseModel):
    zone_ids: list[int]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _block_if_technician(session: Session, user_id: int):
    """Return a 403 response for technician (read-only) logins, else None."""
    row = session.get(CustomUserCustomuser, user_id)
    if row is not None and getattr(row, "is_technician", False):
        return DjangoStyleJSONResponse(
            {"detail": "Read-only (technician) access."}, status_code=403
        )
    return None


def _owned(session: Session, pk: int, user_id: int) -> AnalyticsSector | None:
    return session.scalars(
        select(AnalyticsSector).where(
            AnalyticsSector.id == pk, AnalyticsSector.user_id == user_id
        )
    ).first()


def _zone_count(session: Session, sector_id: int) -> int:
    return int(
        session.scalar(
            select(func.count(AnalyticsZone.id)).where(
                AnalyticsZone.sector_id == sector_id
            )
        )
        or 0
    )


def _serialize(session: Session, s: AnalyticsSector) -> dict[str, Any]:
    return {"id": s.id, "name": s.name, "zone_count": _zone_count(session, s.id)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/sectors", summary="Caller's sectors (id, name, zone_count)")
def list_sectors(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        rows = session.scalars(
            select(AnalyticsSector)
            .where(AnalyticsSector.user_id == user.id)
            .order_by(AnalyticsSector.name)
        ).all()
        return [_serialize(session, s) for s in rows]


@router.post("/sectors", summary="Create a sector")
def create_sector(payload: SectorIn, user: AuthedUser = Depends(get_current_user)):
    with session_scope(commit=True) as session:
        guard = _block_if_technician(session, user.id)
        if guard is not None:
            return guard
        sector = AnalyticsSector(name=payload.name.strip(), user_id=user.id)
        session.add(sector)
        session.flush()
        return DjangoStyleJSONResponse(_serialize(session, sector), status_code=201)


@router.patch("/sectors/{pk}", summary="Rename a sector")
def update_sector(
    pk: int, payload: SectorIn, user: AuthedUser = Depends(get_current_user)
):
    with session_scope(commit=True) as session:
        guard = _block_if_technician(session, user.id)
        if guard is not None:
            return guard
        sector = _owned(session, pk, user.id)
        if sector is None:
            return DjangoStyleJSONResponse(
                {"detail": "Sector not found."}, status_code=404
            )
        sector.name = payload.name.strip()
        session.flush()
        return _serialize(session, sector)


@router.delete("/sectors/{pk}", summary="Delete a sector (unassigns its zones)")
def delete_sector(pk: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope(commit=True) as session:
        guard = _block_if_technician(session, user.id)
        if guard is not None:
            return guard
        sector = _owned(session, pk, user.id)
        if sector is None:
            return DjangoStyleJSONResponse(
                {"detail": "Sector not found."}, status_code=404
            )
        # Unassign this sector's zones (the live column has no DB-level ON DELETE
        # SET NULL — it's ensure-created without the FK — so do it explicitly).
        session.execute(
            update(AnalyticsZone)
            .where(AnalyticsZone.sector_id == pk)
            .values(sector_id=None)
        )
        session.delete(sector)
        return DjangoStyleJSONResponse({"success": True}, status_code=200)


@router.put("/sectors/{pk}/zones", summary="Set exactly which zones are in a sector")
def set_sector_zones(
    pk: int, payload: SectorZonesIn, user: AuthedUser = Depends(get_current_user)
):
    with session_scope(commit=True) as session:
        guard = _block_if_technician(session, user.id)
        if guard is not None:
            return guard
        sector = _owned(session, pk, user.id)
        if sector is None:
            return DjangoStyleJSONResponse(
                {"detail": "Sector not found."}, status_code=404
            )
        # Only the caller's own zones may be (re)assigned.
        owned_ids = set(
            session.scalars(
                select(AnalyticsZone.id).where(AnalyticsZone.user_id == user.id)
            ).all()
        )
        target = {z for z in payload.zone_ids if z in owned_ids}
        # Clear zones currently in this sector, then set the target set.
        session.execute(
            update(AnalyticsZone)
            .where(AnalyticsZone.sector_id == pk)
            .values(sector_id=None)
        )
        if target:
            session.execute(
                update(AnalyticsZone)
                .where(AnalyticsZone.id.in_(target), AnalyticsZone.user_id == user.id)
                .values(sector_id=pk)
            )
        session.flush()
        return _serialize(session, sector)
