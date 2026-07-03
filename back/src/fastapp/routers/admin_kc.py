"""fastapp /admin/kc — cross-user crop-calendar (Kc) management (staff only).

Strangler port of ``apps/irrigation/router_admin_kc.py`` (django-ninja). The
owner-facing ``/kc`` surface is caller-scoped; this admin surface spans ALL
users and adds a ``username`` to each row. Byte-parity with the Django version:
same routes, same serialize shape (owner ``_serialize`` + trailing ``username``),
same 404 envelopes, same period-replace semantics. Reads/writes go through the
agri-core SQLAlchemy session (no Django ORM); the period + zone-ownership
helpers are reused from ``fastapp.routers.kc``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from agri.core.database import session_scope
from agri.db.analytics import AnalyticsKc
from agri.db.users import CustomUserCustomuser
from fastapp.adminutil import record_audit, username_for
from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.json import DjangoStyleJSONResponse
from fastapp.routers.kc import KcPeriodIn, _owns_zone, _replace_periods, _serialize

router = APIRouter(tags=["admin-kc"])


class KcAdminCreateIn(BaseModel):
    username: str
    name: str
    plant_name: str
    zone_id: int | None = None
    periods: list[KcPeriodIn] = []


class KcAdminUpdateIn(BaseModel):
    name: str
    plant_name: str
    zone_id: int | None = None
    periods: list[KcPeriodIn] = []


def _serialize_admin(session, kc: AnalyticsKc) -> dict[str, Any]:
    d = _serialize(session, kc)
    d["username"] = username_for(session, kc.user_id)
    return d


def _resolve_user_id(session, username: str) -> int | None:
    return session.scalar(
        select(CustomUserCustomuser.id).where(CustomUserCustomuser.username == username)
    )


@router.get("/admin/kc", summary="Admin: list crop calendars (Kc)")
def list_kc(
    username: str | None = None,
    zone_id: int | None = None,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope() as session:
        criteria = []
        if username:
            uid = _resolve_user_id(session, username)
            if uid is None:
                return []
            criteria.append(AnalyticsKc.user_id == uid)
        if zone_id is not None:
            criteria.append(AnalyticsKc.zone_id == zone_id)
        rows = session.scalars(
            select(AnalyticsKc).where(*criteria).order_by(AnalyticsKc.id)
        ).all()
        return [_serialize_admin(session, kc) for kc in rows]


@router.post("/admin/kc", summary="Admin: create a crop calendar")
def create_kc(
    payload: KcAdminCreateIn, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        target_id = _resolve_user_id(session, payload.username)
        if target_id is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found."}, status_code=404
            )
        if payload.zone_id is not None and not _owns_zone(
            session, payload.zone_id, target_id
        ):
            return DjangoStyleJSONResponse(
                {"detail": "Zone not found for this user."}, status_code=404
            )
        kc = AnalyticsKc(
            user_id=target_id,
            zone_id=payload.zone_id,
            name=payload.name,
            plant_name=payload.plant_name,
            number_of_periods=0,
        )
        session.add(kc)
        session.flush()
        _replace_periods(session, kc, payload.periods)
        record_audit(
            session, user.id, "kc.create", "kc", kc.id, {"username": payload.username}
        )
        return DjangoStyleJSONResponse(_serialize_admin(session, kc), status_code=201)


@router.get("/admin/kc/{kc_id}", summary="Admin: fetch one crop calendar")
def get_kc(kc_id: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        kc = session.get(AnalyticsKc, kc_id)
        if kc is None:
            return DjangoStyleJSONResponse({"detail": "Kc not found."}, status_code=404)
        return _serialize_admin(session, kc)


@router.put("/admin/kc/{kc_id}", summary="Admin: replace a crop calendar")
def update_kc(
    kc_id: int,
    payload: KcAdminUpdateIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope(commit=True) as session:
        kc = session.get(AnalyticsKc, kc_id)
        if kc is None:
            return DjangoStyleJSONResponse({"detail": "Kc not found."}, status_code=404)
        zone_id = kc.zone_id
        if payload.zone_id is not None:
            if not _owns_zone(session, payload.zone_id, kc.user_id):
                return DjangoStyleJSONResponse(
                    {"detail": "Zone not found for this user."}, status_code=404
                )
            zone_id = payload.zone_id
        kc.name = payload.name
        kc.plant_name = payload.plant_name
        kc.zone_id = zone_id
        session.flush()
        _replace_periods(session, kc, payload.periods)
        record_audit(session, user.id, "kc.update", "kc", kc.id)
        return _serialize_admin(session, kc)


@router.delete("/admin/kc/{kc_id}", summary="Admin: delete a crop calendar")
def delete_kc(kc_id: int, user: AuthedUser = Depends(get_current_staff_user)):
    from agri.db.analytics import AnalyticsKcperiod, AnalyticsKcperiodassignment

    with session_scope(commit=True) as session:
        kc = session.get(AnalyticsKc, kc_id)
        if kc is None:
            return DjangoStyleJSONResponse({"detail": "Kc not found."}, status_code=404)
        assignments = session.scalars(
            select(AnalyticsKcperiodassignment).where(
                AnalyticsKcperiodassignment.kc_id == kc.id
            )
        ).all()
        period_ids = [a.period_id for a in assignments]
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
        record_audit(session, user.id, "kc.delete", "kc", kc_id)
        return DjangoStyleJSONResponse({"status": "deleted"}, status_code=200)
