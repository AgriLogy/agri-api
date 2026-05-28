"""Manager affirmation endpoints (django-ninja).

Migrated from ``analytics.manager_affirmation``:
  * GET  /api/manager-affirmations/?status=pending|approved|rejected
  * POST /api/manager-affirmations/
  * POST /api/manager-affirmations/<pk>/<action>/   (action = approve | reject)

User can list their own / create new; admin sees all. Admin-only for
the decision endpoint.
"""
from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone
from ninja import Router, Schema
from ninja.responses import Response

from agriBack.api.auth import JwtAuth
from analytics.models import ManagerAffirmation

router = Router()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(a: ManagerAffirmation) -> dict[str, Any]:
    return {
        "id": a.id,
        "action": a.action,
        "payload": a.payload,
        "status": a.status,
        "requested_by": a.requested_by_id,
        "requested_by_username": (
            a.requested_by.username if a.requested_by_id else None
        ),
        "decided_by": a.decided_by_id,
        "decided_by_username": a.decided_by.username if a.decided_by_id else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decision_note": a.decision_note,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _require_admin(request) -> Response | None:
    user = request.auth
    if user is None or not getattr(user, "is_staff", False):
        return Response({"detail": "Admin access required"}, status=403)
    return None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ManagerAffirmationIn(Schema):
    """Create body. ``payload`` is opaque JSON (any) until a schema-per-action
    is defined; the legacy DRF serializer also accepted free-form JSON here.
    """

    action: str
    payload: dict[str, Any] | None = None


class DecisionIn(Schema):
    note: str | None = None


# ---------------------------------------------------------------------------
# /api/manager-affirmations/
# ---------------------------------------------------------------------------


@router.get(
    "",
    auth=JwtAuth(),
    summary="List manager affirmations (own; admin sees all)",
)
def list_affirmations(request, status: str | None = None):
    qs = ManagerAffirmation.objects.all()
    if not getattr(request.auth, "is_staff", False):
        qs = qs.filter(requested_by=request.auth)
    if status:
        qs = qs.filter(status=status)
    return [_serialize(a) for a in qs.order_by("-created_at")]


@router.post(
    "",
    auth=JwtAuth(),
    summary="Create a manager affirmation",
)
def create_affirmation(request, payload: ManagerAffirmationIn):
    allowed = dict(ManagerAffirmation.ACTION_CHOICES)
    if payload.action not in allowed:
        return Response(
            {
                "action": (
                    f"Unknown action. Allowed: {sorted(allowed.keys())}."
                ),
            },
            status=400,
        )
    a = ManagerAffirmation.objects.create(
        action=payload.action,
        payload=payload.payload or {},
        requested_by=request.auth,
    )
    return Response(_serialize(a), status=201)


# ---------------------------------------------------------------------------
# /api/manager-affirmations/<pk>/<action>/
# ---------------------------------------------------------------------------


def _decide(request, pk: int, action: str, payload):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    try:
        a = ManagerAffirmation.objects.get(pk=pk)
    except ManagerAffirmation.DoesNotExist:
        return Response({"detail": "Affirmation not found."}, status=404)
    if a.status != ManagerAffirmation.STATUS_PENDING:
        return Response(
            {"detail": f"Already decided ({a.status})."}, status=400,
        )

    a.status = (
        ManagerAffirmation.STATUS_APPROVED
        if action == "approve"
        else ManagerAffirmation.STATUS_REJECTED
    )
    a.decided_by = request.auth
    a.decided_at = timezone.now()
    a.decision_note = payload.note or ""
    a.save(
        update_fields=[
            "status",
            "decided_by",
            "decided_at",
            "decision_note",
            "updated_at",
        ]
    )
    log.info(
        "admin %s %sd affirmation #%s",
        request.auth.username,
        action,
        a.pk,
    )
    return _serialize(a)


@router.post(
    "/{pk}/approve",
    auth=JwtAuth(),
    summary="Admin: approve a manager affirmation",
)
def approve_affirmation(request, pk: int, payload: DecisionIn):
    return _decide(request, pk, "approve", payload)


@router.post(
    "/{pk}/reject",
    auth=JwtAuth(),
    summary="Admin: reject a manager affirmation",
)
def reject_affirmation(request, pk: int, payload: DecisionIn):
    return _decide(request, pk, "reject", payload)
