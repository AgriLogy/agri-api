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

from agriapi.api.auth import JwtAuth
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


@router.get(
    "/zones",
    auth=JwtAuth(),
    summary="Caller's zones (id + name)",
)
def list_my_zones(request):
    qs = Zone.objects.filter(user_id=request.auth.id)
    return [_zone_short(z) for z in qs]


@router.get(
    "/zones/{zone_id}/active-graph",
    auth=JwtAuth(),
    summary="Caller's ActiveGraph config for one zone",
)
def get_my_active_graph(request, zone_id: int):
    try:
        ag = ActiveGraph.objects.get(user=request.auth, zone_id=zone_id)
    except ActiveGraph.DoesNotExist:
        return Response({"detail": "ActiveGraph not found."}, status=404)
    return _active_graph_dict(ag)


# Legacy URL-pattern endpoints removed in the REST-aligned rewrite. The
# admin "view another user's zones" capability now lives at
# /users/<username>/zones (router_admin); the legacy
# /api/active-graph/<u>/<z>/ collapses into
# /users/<u>/zones/<z>/active-graph (also in router_admin).
