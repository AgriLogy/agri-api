"""Admin audit router (django-ninja) — mounted at ``/admin/audit``.

  * GET /admin/audit?actor=&action=&target_type=&limit=

All routes require JWT + ``is_staff``.
"""

from __future__ import annotations

from typing import Any

from ninja import Router

from agriapi.api.auth import JwtAuth
from apps.irrigation.models import AuditEvent
from apps.irrigation.router_admin import _require_admin

router = Router()


def _event(e: AuditEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "actor": getattr(e.actor, "username", None),
        "action": e.action,
        "target_type": e.target_type,
        "target_id": e.target_id,
        "changes": e.changes or {},
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("", auth=JwtAuth(), summary="Admin: list audit events")
def list_audit(
    request,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    limit: int = 200,
):
    guard = _require_admin(request)
    if guard:
        return guard
    qs = AuditEvent.objects.all().order_by("-id")
    if actor:
        qs = qs.filter(actor__username=actor)
    if action:
        qs = qs.filter(action__icontains=action)
    if target_type:
        qs = qs.filter(target_type=target_type)
    limit = max(1, min(limit, 500))
    return [_event(e) for e in qs[:limit]]
