"""Analytics read endpoints (django-ninja).

Migrated from ``analytics.views``:
  * GET /api/header/
  * GET /api/zones-names-per-user/
  * GET /api/active-graph/self/<zone_id>/
  * GET /api/active-zones/<username>/  (admin-only)

The legacy ``active-graph/<username>/<zone_id>/`` admin endpoint stays
DRF for now (defined in ``analytics/adminviews.py``); it's covered by
PR 10. All routes preserve their URL + response shape so the
dashboard's existing parsing stays valid.
"""
from __future__ import annotations

from typing import Any

from django.forms.models import model_to_dict
from ninja import Router
from ninja.responses import Response

from agriBack.api.auth import JwtAuth
from analytics.models import ActiveGraph, Zone
from apps.users.models import CustomUser

router = Router()


def _require_admin(request) -> Response | None:
    user = request.auth
    if user is None or not getattr(user, "is_staff", False):
        return Response({"detail": "Admin access required"}, status=403)
    return None


def _zone_short(z: Zone) -> dict[str, Any]:
    return {"id": z.id, "name": z.name}


def _active_graph_dict(ag: ActiveGraph) -> dict[str, Any]:
    """Match ``ActiveGraphSerializer`` (``exclude=["id"]``)."""
    d = model_to_dict(ag)
    d.pop("id", None)
    return d


@router.get("/header/", auth=JwtAuth(), summary="Authenticated user's identity")
def header(request):
    return {"username": request.auth.username}


@router.get(
    "/zones-names-per-user/",
    auth=JwtAuth(),
    summary="Zones (id+name only) for the caller",
)
def zones_names_per_user(request):
    qs = Zone.objects.filter(user_id=request.auth.id)
    return [_zone_short(z) for z in qs]


@router.get(
    "/active-graph/self/{zone_id}/",
    auth=JwtAuth(),
    summary="Caller's ActiveGraph config for one zone",
)
def active_graph_self(request, zone_id: int):
    try:
        ag = ActiveGraph.objects.get(user=request.auth, zone_id=zone_id)
    except ActiveGraph.DoesNotExist:
        return Response({"detail": "ActiveGraph not found."}, status=404)
    return _active_graph_dict(ag)


@router.get(
    "/active-zones/{username}/",
    auth=JwtAuth(),
    summary="Admin: zones (id+name) for any user by username",
)
def active_zones(request, username: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    try:
        user = CustomUser.objects.get(username=username)
    except CustomUser.DoesNotExist:
        return Response({"detail": "User not found."}, status=404)
    qs = Zone.objects.filter(user=user)
    return [_zone_short(z) for z in qs]
