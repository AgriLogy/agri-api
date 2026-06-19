"""Admin records router (django-ninja) — mounted at ``/admin``.

Read/delete management for per-user records that previously had no admin
surface: notification snapshots, assistant conversations (+ the proactive-notice
ledger), and a GLOBAL view over technician grants (the existing /technicians
router is owner-scoped; this one spans every owner). All routes require JWT +
``is_staff``.

Endpoints:
  * GET                /admin/notifications
  * DELETE             /admin/notifications/{pk}
  * GET                /admin/conversations
  * GET                /admin/conversations/{pk}
  * DELETE             /admin/conversations/{pk}
  * GET                /admin/proactive-notices
  * DELETE             /admin/proactive-notices/{pk}     (reset a user's cooldown)
  * GET                /admin/technician-grants
  * GET                /admin/technician-grants/{pk}
  * PATCH              /admin/technician-grants/{pk}      (toggle is_active)
  * DELETE             /admin/technician-grants/{pk}      (revoke)
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from apps.alerts.models import Notification
from apps.assistant.models import AssistantConversation, ProactiveNotice
from apps.irrigation.audit import record_audit
from apps.irrigation.models import TechnicianGrant
from apps.irrigation.router_admin import _require_admin

router = Router()
log = logging.getLogger(__name__)
User = get_user_model()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


# --- notifications ---------------------------------------------------------
def _notification(n: Notification) -> dict[str, Any]:
    return {
        "id": n.id,
        "username": getattr(n.user, "username", None),
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


@router.get("/notifications", auth=JwtAuth(), summary="Admin: list notifications")
def list_notifications(request, username: str | None = None, limit: int = 200):
    guard = _require_admin(request)
    if guard:
        return guard
    limit = max(1, min(limit, 1000))
    qs = Notification.objects.select_related("user").order_by("-id")
    if username:
        qs = qs.filter(user__username=username)
    return [_notification(n) for n in qs[:limit]]


@router.delete(
    "/notifications/{pk}", auth=JwtAuth(), summary="Admin: delete a notification"
)
def delete_notification(request, pk: int):
    guard = _require_admin(request)
    if guard:
        return guard
    Notification.objects.filter(pk=pk).delete()
    record_audit(request.auth, "notification.delete", "notification", pk)
    return {"status": "deleted"}


# --- assistant conversations ----------------------------------------------
def _conversation(
    c: AssistantConversation, *, include_messages: bool = False
) -> dict[str, Any]:
    msgs = c.messages or []
    row = {
        "id": c.id,
        "username": getattr(c.user, "username", None),
        "client_id": c.client_id,
        "title": c.title,
        "message_count": len(msgs) if isinstance(msgs, list) else 0,
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
    }
    if include_messages:
        row["messages"] = msgs
    return row


@router.get("/conversations", auth=JwtAuth(), summary="Admin: list conversations")
def list_conversations(request, username: str | None = None, limit: int = 200):
    guard = _require_admin(request)
    if guard:
        return guard
    limit = max(1, min(limit, 1000))
    qs = AssistantConversation.objects.select_related("user").order_by("-updated_at")
    if username:
        qs = qs.filter(user__username=username)
    return [_conversation(c) for c in qs[:limit]]


@router.get("/conversations/{pk}", auth=JwtAuth(), summary="Admin: conversation detail")
def get_conversation(request, pk: int):
    guard = _require_admin(request)
    if guard:
        return guard
    c = AssistantConversation.objects.select_related("user").filter(pk=pk).first()
    if c is None:
        return Response({"detail": "Not found"}, status=404)
    return _conversation(c, include_messages=True)


@router.delete(
    "/conversations/{pk}", auth=JwtAuth(), summary="Admin: delete a conversation"
)
def delete_conversation(request, pk: int):
    guard = _require_admin(request)
    if guard:
        return guard
    AssistantConversation.objects.filter(pk=pk).delete()
    record_audit(request.auth, "conversation.delete", "conversation", pk)
    return {"status": "deleted"}


# --- proactive-notice ledger ----------------------------------------------
def _proactive(p: ProactiveNotice) -> dict[str, Any]:
    return {
        "id": p.id,
        "username": getattr(p.user, "username", None),
        "last_sent": _iso(p.last_sent),
    }


@router.get(
    "/proactive-notices", auth=JwtAuth(), summary="Admin: list proactive notices"
)
def list_proactive(request, limit: int = 500):
    guard = _require_admin(request)
    if guard:
        return guard
    limit = max(1, min(limit, 2000))
    qs = ProactiveNotice.objects.select_related("user").order_by("-last_sent")
    return [_proactive(p) for p in qs[:limit]]


@router.delete(
    "/proactive-notices/{pk}",
    auth=JwtAuth(),
    summary="Admin: reset a user's proactive cooldown",
)
def reset_proactive(request, pk: int):
    guard = _require_admin(request)
    if guard:
        return guard
    ProactiveNotice.objects.filter(pk=pk).delete()
    record_audit(request.auth, "proactive_notice.reset", "proactive_notice", pk)
    return {"status": "reset"}


# --- technician grants (global) -------------------------------------------
def _grant(g: TechnicianGrant, *, include_scope: bool = False) -> dict[str, Any]:
    row = {
        "id": g.id,
        "technician": getattr(g.technician, "username", None),
        "owner": getattr(g.owner, "username", None),
        "is_active": g.is_active,
        "created_at": _iso(g.created_at),
    }
    if include_scope:
        row["scope"] = [
            {"zone_id": zg.zone_id, "allowed_graphs": zg.allowed_graphs or []}
            for zg in g.zone_grants.all()
        ]
    return row


class GrantPatchIn(Schema):
    is_active: bool


@router.get(
    "/technician-grants", auth=JwtAuth(), summary="Admin: list ALL technician grants"
)
def list_grants(
    request,
    owner: str | None = None,
    technician: str | None = None,
    limit: int = 500,
):
    guard = _require_admin(request)
    if guard:
        return guard
    limit = max(1, min(limit, 2000))
    qs = TechnicianGrant.objects.select_related("technician", "owner").order_by("-id")
    if owner:
        qs = qs.filter(owner__username=owner)
    if technician:
        qs = qs.filter(technician__username=technician)
    return [_grant(g) for g in qs[:limit]]


@router.get(
    "/technician-grants/{pk}", auth=JwtAuth(), summary="Admin: grant detail + scope"
)
def get_grant(request, pk: int):
    guard = _require_admin(request)
    if guard:
        return guard
    g = (
        TechnicianGrant.objects.select_related("technician", "owner")
        .filter(pk=pk)
        .first()
    )
    if g is None:
        return Response({"detail": "Not found"}, status=404)
    return _grant(g, include_scope=True)


@router.patch(
    "/technician-grants/{pk}",
    auth=JwtAuth(),
    summary="Admin: enable/disable a grant",
)
def patch_grant(request, pk: int, payload: GrantPatchIn):
    guard = _require_admin(request)
    if guard:
        return guard
    g = TechnicianGrant.objects.filter(pk=pk).first()
    if g is None:
        return Response({"detail": "Not found"}, status=404)
    g.is_active = payload.is_active
    g.save(update_fields=["is_active"])
    record_audit(
        request.auth,
        "technician_grant.update",
        "technician_grant",
        pk,
        {"is_active": payload.is_active},
    )
    return _grant(g, include_scope=True)


@router.delete(
    "/technician-grants/{pk}", auth=JwtAuth(), summary="Admin: revoke a grant"
)
def delete_grant(request, pk: int):
    guard = _require_admin(request)
    if guard:
        return guard
    TechnicianGrant.objects.filter(pk=pk).delete()
    record_audit(request.auth, "technician_grant.revoke", "technician_grant", pk)
    return {"status": "revoked"}
