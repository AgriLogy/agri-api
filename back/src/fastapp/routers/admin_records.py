"""fastapp /admin — admin records: notifications, assistant conversations,
proactive-notice ledger, and a GLOBAL view over technician grants (staff only).

Strangler port of ``apps/irrigation/router_records.py`` (django-ninja).
Byte-parity with the Django version: same routes, same serialize shapes, same
ordering, same ``{"status": ...}`` payloads on delete, same 404 envelopes.
Reads/writes go through the agri-core SQLAlchemy session (no Django ORM).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from agri.core.database import session_scope
from agri.db.analytics import AnalyticsNotification
from agri.db.assistant import AssistantConversation, AssistantProactiveNotice
from agri.db.technicians import AnalyticsTechniciangrant, AnalyticsTechnicianzonegrant
from agri.db.users import CustomUserCustomuser
from fastapp.adminutil import record_audit, username_for
from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["admin-records"])


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _resolve_user_id(session, username: str) -> int:
    """username → id, or a sentinel that matches no row (empty filter result)."""
    uid = session.scalar(
        select(CustomUserCustomuser.id).where(CustomUserCustomuser.username == username)
    )
    return uid if uid is not None else -1


# --- notifications ---------------------------------------------------------
def _notification(session, n: AnalyticsNotification) -> dict[str, Any]:
    return {
        "id": n.id,
        "username": username_for(session, n.user_id),
        "today_temperature": float(n.today_temperature)
        if n.today_temperature is not None
        else None,
        "today_humidity": float(n.today_humidity)
        if n.today_humidity is not None
        else None,
        "ET0": float(n.ET0) if n.ET0 is not None else None,
        "soil_humidity": float(n.soil_humidity)
        if n.soil_humidity is not None
        else None,
        "perfect_irrigation_period": n.perfect_irrigation_period,
        "last_irrigation_date": n.last_irrigation_date.isoformat()
        if n.last_irrigation_date
        else None,
        "notification_date": _iso(n.notification_date),
    }


@router.get("/admin/notifications", summary="Admin: list notifications")
def list_notifications(
    username: str | None = None,
    limit: int = 200,
    user: AuthedUser = Depends(get_current_staff_user),
):
    limit = max(1, min(limit, 1000))
    with session_scope() as session:
        criteria = []
        if username:
            criteria.append(
                AnalyticsNotification.user_id == _resolve_user_id(session, username)
            )
        rows = session.scalars(
            select(AnalyticsNotification)
            .where(*criteria)
            .order_by(AnalyticsNotification.id.desc())
            .limit(limit)
        ).all()
        return [_notification(session, n) for n in rows]


@router.delete("/admin/notifications/{pk}", summary="Admin: delete a notification")
def delete_notification(pk: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope(commit=True) as session:
        n = session.get(AnalyticsNotification, pk)
        if n is not None:
            session.delete(n)
        record_audit(session, user.id, "notification.delete", "notification", pk)
        return {"status": "deleted"}


# --- assistant conversations ----------------------------------------------
def _conversation(
    session, c: AssistantConversation, *, include_messages: bool = False
) -> dict[str, Any]:
    msgs = c.messages or []
    row = {
        "id": c.id,
        "username": username_for(session, c.user_id),
        "client_id": c.client_id,
        "title": c.title,
        "message_count": len(msgs) if isinstance(msgs, list) else 0,
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
    }
    if include_messages:
        row["messages"] = msgs
    return row


@router.get("/admin/conversations", summary="Admin: list conversations")
def list_conversations(
    username: str | None = None,
    limit: int = 200,
    user: AuthedUser = Depends(get_current_staff_user),
):
    limit = max(1, min(limit, 1000))
    with session_scope() as session:
        criteria = []
        if username:
            criteria.append(
                AssistantConversation.user_id == _resolve_user_id(session, username)
            )
        rows = session.scalars(
            select(AssistantConversation)
            .where(*criteria)
            .order_by(AssistantConversation.updated_at.desc())
            .limit(limit)
        ).all()
        return [_conversation(session, c) for c in rows]


@router.get("/admin/conversations/{pk}", summary="Admin: conversation detail")
def get_conversation(pk: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        c = session.get(AssistantConversation, pk)
        if c is None:
            return DjangoStyleJSONResponse({"detail": "Not found"}, status_code=404)
        return _conversation(session, c, include_messages=True)


@router.delete("/admin/conversations/{pk}", summary="Admin: delete a conversation")
def delete_conversation(pk: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope(commit=True) as session:
        c = session.get(AssistantConversation, pk)
        if c is not None:
            session.delete(c)
        record_audit(session, user.id, "conversation.delete", "conversation", pk)
        return {"status": "deleted"}


# --- proactive-notice ledger ----------------------------------------------
def _proactive(session, p: AssistantProactiveNotice) -> dict[str, Any]:
    return {
        "id": p.id,
        "username": username_for(session, p.user_id),
        "last_sent": _iso(p.last_sent),
    }


@router.get("/admin/proactive-notices", summary="Admin: list proactive notices")
def list_proactive(
    limit: int = 500, user: AuthedUser = Depends(get_current_staff_user)
):
    limit = max(1, min(limit, 2000))
    with session_scope() as session:
        rows = session.scalars(
            select(AssistantProactiveNotice)
            .order_by(AssistantProactiveNotice.last_sent.desc())
            .limit(limit)
        ).all()
        return [_proactive(session, p) for p in rows]


@router.delete(
    "/admin/proactive-notices/{pk}",
    summary="Admin: reset a user's proactive cooldown",
)
def reset_proactive(pk: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope(commit=True) as session:
        p = session.get(AssistantProactiveNotice, pk)
        if p is not None:
            session.delete(p)
        record_audit(session, user.id, "proactive_notice.reset", "proactive_notice", pk)
        return {"status": "reset"}


# --- technician grants (global) -------------------------------------------
class GrantPatchIn(BaseModel):
    is_active: bool


def _grant(
    session, g: AnalyticsTechniciangrant, *, include_scope: bool = False
) -> dict[str, Any]:
    row = {
        "id": g.id,
        "technician": username_for(session, g.technician_id),
        "owner": username_for(session, g.owner_id),
        "is_active": g.is_active,
        "created_at": _iso(g.created_at),
    }
    if include_scope:
        zgs = session.scalars(
            select(AnalyticsTechnicianzonegrant)
            .where(AnalyticsTechnicianzonegrant.grant_id == g.id)
            .order_by(AnalyticsTechnicianzonegrant.id)
        ).all()
        row["scope"] = [
            {"zone_id": zg.zone_id, "allowed_graphs": zg.allowed_graphs or []}
            for zg in zgs
        ]
    return row


@router.get("/admin/technician-grants", summary="Admin: list ALL technician grants")
def list_grants(
    owner: str | None = None,
    technician: str | None = None,
    limit: int = 500,
    user: AuthedUser = Depends(get_current_staff_user),
):
    limit = max(1, min(limit, 2000))
    with session_scope() as session:
        criteria = []
        if owner:
            criteria.append(
                AnalyticsTechniciangrant.owner_id == _resolve_user_id(session, owner)
            )
        if technician:
            criteria.append(
                AnalyticsTechniciangrant.technician_id
                == _resolve_user_id(session, technician)
            )
        rows = session.scalars(
            select(AnalyticsTechniciangrant)
            .where(*criteria)
            .order_by(AnalyticsTechniciangrant.id.desc())
            .limit(limit)
        ).all()
        return [_grant(session, g) for g in rows]


@router.get("/admin/technician-grants/{pk}", summary="Admin: grant detail + scope")
def get_grant(pk: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        g = session.get(AnalyticsTechniciangrant, pk)
        if g is None:
            return DjangoStyleJSONResponse({"detail": "Not found"}, status_code=404)
        return _grant(session, g, include_scope=True)


@router.patch("/admin/technician-grants/{pk}", summary="Admin: enable/disable a grant")
def patch_grant(
    pk: int,
    payload: GrantPatchIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope(commit=True) as session:
        g = session.get(AnalyticsTechniciangrant, pk)
        if g is None:
            return DjangoStyleJSONResponse({"detail": "Not found"}, status_code=404)
        g.is_active = payload.is_active
        session.flush()
        record_audit(
            session,
            user.id,
            "technician_grant.update",
            "technician_grant",
            pk,
            {"is_active": payload.is_active},
        )
        return _grant(session, g, include_scope=True)


@router.delete("/admin/technician-grants/{pk}", summary="Admin: revoke a grant")
def delete_grant(pk: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope(commit=True) as session:
        g = session.get(AnalyticsTechniciangrant, pk)
        if g is not None:
            session.delete(g)
        record_audit(
            session, user.id, "technician_grant.revoke", "technician_grant", pk
        )
        return {"status": "revoked"}
