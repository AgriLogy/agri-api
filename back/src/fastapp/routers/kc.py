"""fastapp /kc — crop-calendar (Kc) CRUD.

Strangler port of ``apps/irrigation/router_kc.py`` (django-ninja). Byte-parity
with the Django version: same routes, same serialize shape (periods sorted by
``start_date``), same technician block, same 404 envelopes, same
``{"status": "deleted"}`` on delete.

Writes are direct + caller-scoped over the agri-core SQLAlchemy session (no
Django ORM); a Kc's periods are fully replaced on every write, exactly like
the Django ``_replace_periods``.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from agri.core.database import session_scope
from agri.db.analytics import (
    AnalyticsKc,
    AnalyticsKcperiod,
    AnalyticsKcperiodassignment,
    AnalyticsZone,
)
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["kc"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class KcPeriodIn(BaseModel):
    period_name: str
    start_date: datetime.date
    end_date: datetime.date
    kc_value: float


class KcWriteIn(BaseModel):
    """Create/replace body. ``periods`` fully replaces the crop's stages."""

    name: str
    plant_name: str
    zone_id: int | None = None
    periods: list[KcPeriodIn] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_if_technician(user: AuthedUser) -> None:
    if user.is_technician:
        raise HTTPException(status_code=403, detail="Read-only (technician) access.")


def _serialize(session, kc: AnalyticsKc) -> dict[str, Any]:
    rows = session.execute(
        select(AnalyticsKcperiod)
        .join(
            AnalyticsKcperiodassignment,
            AnalyticsKcperiodassignment.period_id == AnalyticsKcperiod.id,
        )
        .where(AnalyticsKcperiodassignment.kc_id == kc.id)
        .order_by(AnalyticsKcperiodassignment.id)
    ).scalars()
    periods = [
        {
            "id": p.id,
            "period_name": p.period_name,
            "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat(),
            "kc_value": p.kc_value,
        }
        for p in rows
    ]
    periods.sort(key=lambda p: p["start_date"])
    zone_name = None
    if kc.zone_id:
        zone = session.get(AnalyticsZone, kc.zone_id)
        zone_name = zone.name if zone is not None else None
    return {
        "id": kc.id,
        "name": kc.name,
        "plant_name": kc.plant_name,
        "zone_id": kc.zone_id,
        "zone_name": zone_name,
        "number_of_periods": kc.number_of_periods,
        "periods": periods,
    }


def _replace_periods(session, kc: AnalyticsKc, periods: list[KcPeriodIn]) -> None:
    """Drop the crop's existing stages and recreate them from ``periods``."""
    old = session.scalars(
        select(AnalyticsKcperiodassignment).where(
            AnalyticsKcperiodassignment.kc_id == kc.id
        )
    ).all()
    old_period_ids = [a.period_id for a in old]
    for a in old:
        session.delete(a)
    session.flush()
    if old_period_ids:
        for p in session.scalars(
            select(AnalyticsKcperiod).where(AnalyticsKcperiod.id.in_(old_period_ids))
        ).all():
            session.delete(p)
        session.flush()
    for p in periods:
        period = AnalyticsKcperiod(
            period_name=p.period_name,
            start_date=p.start_date,
            end_date=p.end_date,
            kc_value=p.kc_value,
        )
        session.add(period)
        session.flush()
        session.add(AnalyticsKcperiodassignment(kc_id=kc.id, period_id=period.id))
    kc.number_of_periods = len(periods)
    session.flush()


def _owned(session, kc_id: int, user_id: int) -> AnalyticsKc | None:
    return session.scalars(
        select(AnalyticsKc).where(
            AnalyticsKc.id == kc_id, AnalyticsKc.user_id == user_id
        )
    ).first()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/kc", summary="List the caller's crop calendars (Kc)")
def list_kc(zone_id: int | None = None, user: AuthedUser = Depends(get_current_user)):
    _block_if_technician(user)
    criteria = [AnalyticsKc.user_id == user.id]
    if zone_id is not None:
        criteria.append(AnalyticsKc.zone_id == zone_id)
    with session_scope() as session:
        rows = session.scalars(
            select(AnalyticsKc).where(*criteria).order_by(AnalyticsKc.id)
        ).all()
        return [_serialize(session, kc) for kc in rows]


@router.post("/kc", summary="Create a crop calendar (Kc)")
def create_kc(payload: KcWriteIn, user: AuthedUser = Depends(get_current_user)):
    _block_if_technician(user)
    with session_scope(commit=True) as session:
        if payload.zone_id is not None and not _owns_zone(
            session, payload.zone_id, user.id
        ):
            return DjangoStyleJSONResponse(
                {"detail": "Zone not found."}, status_code=404
            )
        kc = AnalyticsKc(
            user_id=user.id,
            zone_id=payload.zone_id,
            name=payload.name,
            plant_name=payload.plant_name,
            number_of_periods=0,
        )
        session.add(kc)
        session.flush()
        _replace_periods(session, kc, payload.periods)
        return DjangoStyleJSONResponse(_serialize(session, kc), status_code=201)


@router.get("/kc/{kc_id}", summary="Fetch one crop calendar")
def get_kc(kc_id: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        kc = _owned(session, kc_id, user.id)
        if kc is None:
            return DjangoStyleJSONResponse({"detail": "Kc not found."}, status_code=404)
        return _serialize(session, kc)


@router.put("/kc/{kc_id}", summary="Replace one crop calendar")
def update_kc(
    kc_id: int, payload: KcWriteIn, user: AuthedUser = Depends(get_current_user)
):
    with session_scope(commit=True) as session:
        kc = _owned(session, kc_id, user.id)
        if kc is None:
            return DjangoStyleJSONResponse({"detail": "Kc not found."}, status_code=404)
        zone_id = kc.zone_id
        if payload.zone_id is not None:
            if not _owns_zone(session, payload.zone_id, user.id):
                return DjangoStyleJSONResponse(
                    {"detail": "Zone not found."}, status_code=404
                )
            zone_id = payload.zone_id
        kc.name = payload.name
        kc.plant_name = payload.plant_name
        kc.zone_id = zone_id
        session.flush()
        _replace_periods(session, kc, payload.periods)
        return _serialize(session, kc)


@router.delete("/kc/{kc_id}", summary="Delete a crop calendar")
def delete_kc(kc_id: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope(commit=True) as session:
        kc = _owned(session, kc_id, user.id)
        if kc is None:
            return DjangoStyleJSONResponse({"detail": "Kc not found."}, status_code=404)
        assignments = session.scalars(
            select(AnalyticsKcperiodassignment).where(
                AnalyticsKcperiodassignment.kc_id == kc.id
            )
        ).all()
        period_ids = [a.period_id for a in assignments]
        # Delete children explicitly (no DB-level ON DELETE CASCADE; the Django
        # side relies on the ORM to cascade). Assignments first so deleting the
        # Kc never leaves a dangling FK or triggers an ORM null-out.
        for a in assignments:
            session.delete(a)
        session.flush()
        session.delete(kc)
        session.flush()
        if period_ids:
            for p in session.scalars(
                select(AnalyticsKcperiod).where(AnalyticsKcperiod.id.in_(period_ids))
            ).all():
                session.delete(p)
    return DjangoStyleJSONResponse({"status": "deleted"}, status_code=200)


def _owns_zone(session, zone_id: int, user_id: int) -> bool:
    return (
        session.scalars(
            select(AnalyticsZone.id)
            .where(AnalyticsZone.id == zone_id, AnalyticsZone.user_id == user_id)
            .limit(1)
        ).first()
        is not None
    )
