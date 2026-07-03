"""fastapp /admin/settings — system settings (staff only).

Strangler port of ``apps/irrigation/router_settings.py`` (django-ninja).
Byte-parity with the Django version: same routes, same default seeding on GET,
same ``{category: [{key, value}]}`` grouping (ordered by category, key), same
400 / 409 / 404 envelopes. Reads/writes go through the agri-core SQLAlchemy
session (no Django ORM).
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from agri.core.database import session_scope
from agri.db.audit import AnalyticsSystemsetting
from fastapp.adminutil import record_audit
from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["admin-settings"])


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# Sensible defaults, seeded lazily on first GET (mirror the Django list).
DEFAULT_SETTINGS: list[dict[str, Any]] = [
    {
        "key": "support_email",
        "category": "general",
        "value": "contact.agrilogy@gmail.com",
    },
    {"key": "notify_email_enabled", "category": "notifications", "value": True},
    {"key": "notify_sms_enabled", "category": "notifications", "value": False},
    {"key": "notify_whatsapp_enabled", "category": "notifications", "value": False},
    {"key": "default_critical_moisture_pct", "category": "thresholds", "value": 20},
    {"key": "stale_device_hours", "category": "thresholds", "value": 24},
]


class SettingsPatchIn(BaseModel):
    values: dict[str, Any] = {}


class SettingCreateIn(BaseModel):
    key: str
    value: Any = ""
    category: str = "general"


def _seed_defaults(session) -> None:
    existing = set(session.scalars(select(AnalyticsSystemsetting.key)).all())
    for d in DEFAULT_SETTINGS:
        if d["key"] not in existing:
            session.add(
                AnalyticsSystemsetting(
                    key=d["key"],
                    value=d["value"],
                    category=d["category"],
                    updated_at=_utcnow(),
                )
            )
    session.flush()


def _grouped(session) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    rows = session.scalars(
        select(AnalyticsSystemsetting).order_by(
            AnalyticsSystemsetting.category, AnalyticsSystemsetting.key
        )
    ).all()
    for s in rows:
        out.setdefault(s.category or "general", []).append(
            {"key": s.key, "value": s.value}
        )
    return out


@router.get("/admin/settings", summary="Admin: get system settings")
def get_settings(user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope(commit=True) as session:
        _seed_defaults(session)
        return _grouped(session)


@router.patch("/admin/settings", summary="Admin: update system settings")
def patch_settings(
    payload: SettingsPatchIn, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        for key, value in (payload.values or {}).items():
            existing = session.scalars(
                select(AnalyticsSystemsetting).where(AnalyticsSystemsetting.key == key)
            ).first()
            if existing is not None:
                existing.value = value
                existing.updated_at = _utcnow()
            else:
                session.add(
                    AnalyticsSystemsetting(
                        key=key,
                        value=value,
                        category="general",
                        updated_at=_utcnow(),
                    )
                )
        session.flush()
        record_audit(
            session, user.id, "settings.update", "settings", "", payload.values or {}
        )
        return _grouped(session)


@router.post("/admin/settings", summary="Admin: create a system setting key")
def create_setting(
    payload: SettingCreateIn, user: AuthedUser = Depends(get_current_staff_user)
):
    key = (payload.key or "").strip()
    if not key:
        return DjangoStyleJSONResponse({"detail": "key is required"}, status_code=400)
    with session_scope(commit=True) as session:
        exists = session.scalars(
            select(AnalyticsSystemsetting.id).where(AnalyticsSystemsetting.key == key)
        ).first()
        if exists is not None:
            return DjangoStyleJSONResponse(
                {"detail": "Setting already exists"}, status_code=409
            )
        session.add(
            AnalyticsSystemsetting(
                key=key,
                value=payload.value,
                category=(payload.category or "general"),
                updated_at=_utcnow(),
            )
        )
        session.flush()
        record_audit(
            session,
            user.id,
            "settings.create",
            "settings",
            key,
            {"category": payload.category, "value": payload.value},
        )
        return _grouped(session)


@router.delete("/admin/settings/{key}", summary="Admin: delete a system setting key")
def delete_setting(key: str, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope(commit=True) as session:
        existing = session.scalars(
            select(AnalyticsSystemsetting).where(AnalyticsSystemsetting.key == key)
        ).first()
        if existing is None:
            return DjangoStyleJSONResponse({"detail": "Not found"}, status_code=404)
        session.delete(existing)
        session.flush()
        record_audit(session, user.id, "settings.delete", "settings", key)
        return _grouped(session)
