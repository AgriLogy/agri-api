"""Shared helpers for the fastapp business-admin routers (F6).

Username resolution + best-effort audit recording, both over the agri-core
SQLAlchemy session (no Django ORM). ``record_audit`` mirrors the Django
``apps.irrigation.audit.record_audit`` write (an ``analytics_auditevent`` row per
admin mutation); it is written in the caller's transaction — the audit table is
always deployed, so this never fails in practice.
"""

from __future__ import annotations

import datetime
from typing import Any, Iterable

from sqlalchemy import select

from agri.db.audit import AnalyticsAuditevent
from agri.db.users import CustomUserCustomuser


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def username_for(session, user_id: int | None) -> str | None:
    """The username for ``user_id`` (``None`` when unset or the row is gone) —
    matches Django's ``getattr(obj.user, "username", None)``."""
    if user_id is None:
        return None
    user = session.get(CustomUserCustomuser, user_id)
    return user.username if user is not None else None


def usernames_for(session, user_ids: Iterable[int | None]) -> dict[int, str]:
    """Batch username lookup for a set of ids (skips ``None``)."""
    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return {}
    rows = session.execute(
        select(CustomUserCustomuser.id, CustomUserCustomuser.username).where(
            CustomUserCustomuser.id.in_(ids)
        )
    ).all()
    return {rid: uname for rid, uname in rows}


def record_audit(
    session,
    actor_id: int | None,
    action: str,
    target_type: str = "",
    target_id: Any = "",
    changes: dict | None = None,
) -> None:
    """Record an admin action (mirrors Django ``record_audit``)."""
    session.add(
        AnalyticsAuditevent(
            actor_id=actor_id,
            action=action[:64],
            target_type=(target_type or "")[:64],
            target_id=str(target_id or "")[:64],
            changes=changes or {},
            created_at=_utcnow(),
        )
    )
