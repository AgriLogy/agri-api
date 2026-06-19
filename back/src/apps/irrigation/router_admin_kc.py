"""Admin crop-calendar (Kc) router (django-ninja) — mounted at ``/admin/kc``.

The owner-facing ``/kc`` router (apps.irrigation.router_kc) is caller-scoped:
a user only sees their own crop calendars. This admin surface lets staff view
and manage Kc sets across ALL users. All routes require JWT + ``is_staff``.

  * GET    /admin/kc?username=&zone_id=   → list (optionally filtered)
  * POST   /admin/kc                      → create for a given username
  * GET    /admin/kc/{kc_id}              → detail
  * PUT    /admin/kc/{kc_id}              → replace (name/plant/zone/periods)
  * DELETE /admin/kc/{kc_id}              → delete (+ its periods)
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from analytics.models import Kc, KcPeriod, Zone
from apps.irrigation.audit import record_audit
from apps.irrigation.router_admin import _require_admin
from apps.irrigation.router_kc import KcPeriodIn, _replace_periods, _serialize

router = Router()
User = get_user_model()


class KcAdminCreateIn(Schema):
    """Admin create body — like the owner KcWriteIn but names the target user."""

    username: str
    name: str
    plant_name: str
    zone_id: int | None = None
    periods: list[KcPeriodIn] = []


class KcAdminUpdateIn(Schema):
    name: str
    plant_name: str
    zone_id: int | None = None
    periods: list[KcPeriodIn] = []


def _serialize_admin(kc: Kc) -> dict:
    d = _serialize(kc)
    d["username"] = getattr(kc.user, "username", None)
    return d


@router.get("", auth=JwtAuth(), summary="Admin: list crop calendars (Kc)")
def list_kc(request, username: str | None = None, zone_id: int | None = None):
    guard = _require_admin(request)
    if guard:
        return guard
    qs = Kc.objects.select_related("zone", "user").order_by("id")
    if username:
        qs = qs.filter(user__username=username)
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)
    return [_serialize_admin(kc) for kc in qs]


@router.post("", auth=JwtAuth(), summary="Admin: create a crop calendar")
def create_kc(request, payload: KcAdminCreateIn):
    guard = _require_admin(request)
    if guard:
        return guard
    user = User.objects.filter(username=payload.username).first()
    if user is None:
        return Response({"detail": "User not found."}, status=404)
    zone = None
    if payload.zone_id is not None:
        zone = Zone.objects.filter(id=payload.zone_id, user=user).first()
        if zone is None:
            return Response({"detail": "Zone not found for this user."}, status=404)
    with transaction.atomic():
        kc = Kc.objects.create(
            user=user,
            zone=zone,
            name=payload.name,
            plant_name=payload.plant_name,
            number_of_periods=0,
        )
        _replace_periods(kc, payload.periods)
    record_audit(request.auth, "kc.create", "kc", kc.id, {"username": payload.username})
    return Response(_serialize_admin(kc), status=201)


@router.get("/{kc_id}", auth=JwtAuth(), summary="Admin: fetch one crop calendar")
def get_kc(request, kc_id: int):
    guard = _require_admin(request)
    if guard:
        return guard
    kc = Kc.objects.select_related("zone", "user").filter(id=kc_id).first()
    if kc is None:
        return Response({"detail": "Kc not found."}, status=404)
    return _serialize_admin(kc)


@router.put("/{kc_id}", auth=JwtAuth(), summary="Admin: replace a crop calendar")
def update_kc(request, kc_id: int, payload: KcAdminUpdateIn):
    guard = _require_admin(request)
    if guard:
        return guard
    kc = Kc.objects.select_related("zone", "user").filter(id=kc_id).first()
    if kc is None:
        return Response({"detail": "Kc not found."}, status=404)
    zone = kc.zone
    if payload.zone_id is not None:
        zone = Zone.objects.filter(id=payload.zone_id, user=kc.user).first()
        if zone is None:
            return Response({"detail": "Zone not found for this user."}, status=404)
    with transaction.atomic():
        kc.name = payload.name
        kc.plant_name = payload.plant_name
        kc.zone = zone
        kc.save(update_fields=["name", "plant_name", "zone"])
        _replace_periods(kc, payload.periods)
    record_audit(request.auth, "kc.update", "kc", kc.id)
    return _serialize_admin(kc)


@router.delete("/{kc_id}", auth=JwtAuth(), summary="Admin: delete a crop calendar")
def delete_kc(request, kc_id: int):
    guard = _require_admin(request)
    if guard:
        return guard
    kc = Kc.objects.filter(id=kc_id).first()
    if kc is None:
        return Response({"detail": "Kc not found."}, status=404)
    period_ids = list(kc.periods.values_list("period_id", flat=True))  # type: ignore[attr-defined]
    kc.delete()
    KcPeriod.objects.filter(id__in=period_ids).delete()
    record_audit(request.auth, "kc.delete", "kc", kc_id)
    return Response({"status": "deleted"}, status=200)
