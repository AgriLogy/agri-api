"""fastapp /admin/audit — admin audit-event log (staff only).

Strangler port of ``apps/irrigation/router_audit.py`` (django-ninja). Byte-parity
with the Django version: same single ``GET /admin/audit`` route, same filters
(``actor`` / ``action`` icontains / ``target_type`` / ``limit``), same serialize
shape, same ``-id`` order. Reads go through the agri-core SQLAlchemy session
(no Django ORM).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from agri.core.database import session_scope
from agri.db.audit import AnalyticsAuditevent
from fastapp.adminutil import username_for
from fastapp.auth import AuthedUser, get_current_staff_user

router = APIRouter(tags=["admin-audit"])


def _event(session, e: AnalyticsAuditevent) -> dict[str, Any]:
    return {
        "id": e.id,
        "actor": username_for(session, e.actor_id),
        "action": e.action,
        "target_type": e.target_type,
        "target_id": e.target_id,
        "changes": e.changes or {},
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("/admin/audit", summary="Admin: list audit events")
def list_audit(
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    limit: int = 200,
    user: AuthedUser = Depends(get_current_staff_user),
):
    limit = max(1, min(limit, 500))
    with session_scope() as session:
        criteria = []
        if actor:
            # Django filters ``actor__username=actor`` — resolve to id(s).
            from agri.db.users import CustomUserCustomuser

            actor_ids = session.scalars(
                select(CustomUserCustomuser.id).where(
                    CustomUserCustomuser.username == actor
                )
            ).all()
            criteria.append(AnalyticsAuditevent.actor_id.in_(actor_ids))
        if action:
            criteria.append(AnalyticsAuditevent.action.icontains(action))
        if target_type:
            criteria.append(AnalyticsAuditevent.target_type == target_type)
        rows = session.scalars(
            select(AnalyticsAuditevent)
            .where(*criteria)
            .order_by(AnalyticsAuditevent.id.desc())
            .limit(limit)
        ).all()
        return [_event(session, e) for e in rows]
