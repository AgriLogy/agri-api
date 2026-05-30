"""Admin analytics router (django-ninja).

Mounted at ``/api/admin``. Endpoints:

  * GET   /api/admin/overview/
  * GET, POST            /api/admin/users/<username>/zones/
  * GET, PUT, PATCH, DELETE  /api/admin/users/<username>/zones/<pk>/
  * GET, PUT, PATCH      /api/admin/users/<username>/zones/<pk>/params/
  * GET, PATCH           /api/admin/users/<username>/zones/<zone_id>/active-graph/
  * GET                  /api/admin/users/<username>/alerts/
  * GET, PATCH, DELETE   /api/admin/alerts/<pk>/
  * GET                  /api/admin/users/<username>/activity/
  * GET, PATCH           /api/admin/users/<username>/sensor-units/

Plus the legacy ``GET, PUT /api/active-graph/<username>/<zone_id>/``
from ``analytics/adminviews.py`` (the in-flight migration path).

All routes require JWT + ``is_staff`` (checked inline).
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.forms.models import model_to_dict
from django.utils import timezone
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from analytics.models import ActiveGraph, Alert, Zone

router = Router()
log = logging.getLogger(__name__)
User = get_user_model()


def _require_admin(request) -> Response | None:
    user = request.auth
    if user is None or not getattr(user, "is_staff", False):
        return Response({"detail": "Admin access required"}, status=403)
    return None


def _resolve_user(username: str):
    return User.objects.filter(username=username).first()


# ---------------------------------------------------------------------------
# Zone serialization
# ---------------------------------------------------------------------------

ZONE_FIELDS = [
    "id",
    "name",
    "space",
    "soil_param_TAW",
    "soil_param_FC",
    "soil_param_WP",
    "soil_param_RAW",
    "critical_moisture_threshold",
    "pomp_flow_rate",
    "irrigation_water_quantity",
]

ZONE_PARAMS_FIELDS = [
    "id",
    "soil_param_TAW",
    "soil_param_FC",
    "soil_param_WP",
    "soil_param_RAW",
    "critical_moisture_threshold",
    "pomp_flow_rate",
    "irrigation_water_quantity",
]


def _serialize_zone(z: Zone) -> dict[str, Any]:
    return {f: getattr(z, f) for f in ZONE_FIELDS}


def _serialize_zone_params(z: Zone) -> dict[str, Any]:
    return {f: getattr(z, f) for f in ZONE_PARAMS_FIELDS}


def _validate_zone(payload: dict[str, Any]) -> dict | None:
    if "space" in payload and payload["space"] is not None and payload["space"] <= 0:
        return {"space": "Space must be strictly positive."}
    cmt = payload.get("critical_moisture_threshold")
    if cmt is not None and (cmt < 0 or cmt > 100):
        return {"critical_moisture_threshold": "Threshold must be between 0 and 100."}
    pfr = payload.get("pomp_flow_rate")
    if pfr is not None and pfr < 0:
        return {"pomp_flow_rate": "Flow rate must be non-negative."}
    fc = payload.get("soil_param_FC")
    wp = payload.get("soil_param_WP")
    if fc is not None and wp is not None and fc < wp:
        return {
            "non_field_errors": (
                "Field capacity (FC) cannot be lower than wilting point (WP)."
            )
        }
    return None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ZoneWriteIn(Schema):
    name: str | None = None
    space: float | None = None
    soil_param_TAW: float | None = None
    soil_param_FC: float | None = None
    soil_param_WP: float | None = None
    soil_param_RAW: float | None = None
    critical_moisture_threshold: float | None = None
    pomp_flow_rate: float | None = None
    irrigation_water_quantity: float | None = None


class AlertAdminPatchIn(Schema):
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@router.get("/admin/overview", auth=JwtAuth(), summary="Admin: dashboard KPIs")
def overview(request):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    since = timezone.now() - timezone.timedelta(hours=24)
    return {
        "users_total": User.objects.count(),
        "users_active": User.objects.filter(is_active=True).count(),
        "staff_total": User.objects.filter(is_staff=True).count(),
        "zones_total": Zone.objects.count(),
        "alerts_24h": Alert.objects.filter(last_triggered_at__gte=since).count(),
    }


# ---------------------------------------------------------------------------
# Zones CRUD per user
# ---------------------------------------------------------------------------


@router.get(
    "/users/{username}/zones",
    auth=JwtAuth(),
    summary="Admin: list a user's zones",
)
def list_user_zones(request, username: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user = _resolve_user(username)
    if user is None:
        return Response({"detail": f"User '{username}' not found."}, status=404)
    return [_serialize_zone(z) for z in Zone.objects.filter(user=user).order_by("id")]


@router.post(
    "/users/{username}/zones",
    auth=JwtAuth(),
    summary="Admin: create a zone for a user",
)
def create_user_zone(request, username: str, payload: ZoneWriteIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user = _resolve_user(username)
    if user is None:
        return Response({"detail": f"User '{username}' not found."}, status=404)
    data = payload.model_dump(exclude_unset=True)
    err = _validate_zone(data)
    if err is not None:
        return Response(err, status=400)
    z = Zone.objects.create(user=user, **data)
    return Response(_serialize_zone(z), status=201)


def _get_user_zone(username: str, pk: int) -> tuple[Zone | None, Response | None]:
    user = _resolve_user(username)
    if user is None:
        return None, Response({"detail": f"User '{username}' not found."}, status=404)
    z = Zone.objects.filter(user=user, pk=pk).first()
    if z is None:
        return None, Response({"detail": "Zone not found for this user."}, status=404)
    return z, None


@router.get(
    "/users/{username}/zones/{pk}",
    auth=JwtAuth(),
    summary="Admin: fetch a user's zone",
)
def get_user_zone(request, username: str, pk: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    z, err = _get_user_zone(username, pk)
    if err is not None:
        return err
    return _serialize_zone(z)


def _update_zone(request, username: str, pk: int, payload: ZoneWriteIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    z, err = _get_user_zone(username, pk)
    if err is not None:
        return err
    data = payload.model_dump(exclude_unset=True)
    bad = _validate_zone(data)
    if bad is not None:
        return Response(bad, status=400)
    for k, v in data.items():
        setattr(z, k, v)
    z.save()
    return _serialize_zone(z)


@router.put("/users/{username}/zones/{pk}", auth=JwtAuth())
def put_user_zone(request, username: str, pk: int, payload: ZoneWriteIn):
    return _update_zone(request, username, pk, payload)


@router.patch("/users/{username}/zones/{pk}", auth=JwtAuth())
def patch_user_zone(request, username: str, pk: int, payload: ZoneWriteIn):
    return _update_zone(request, username, pk, payload)


@router.delete("/users/{username}/zones/{pk}", auth=JwtAuth())
def delete_user_zone(request, username: str, pk: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    z, err = _get_user_zone(username, pk)
    if err is not None:
        return err
    z.delete()
    return Response(None, status=204)


# ---------------------------------------------------------------------------
# Zone params subset
# ---------------------------------------------------------------------------


@router.get("/users/{username}/zones/{pk}/params", auth=JwtAuth())
def get_zone_params(request, username: str, pk: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    z, err = _get_user_zone(username, pk)
    if err is not None:
        return err
    return _serialize_zone_params(z)


def _update_zone_params(request, username: str, pk: int, payload: ZoneWriteIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    z, err = _get_user_zone(username, pk)
    if err is not None:
        return err
    data = {
        k: v
        for k, v in payload.model_dump(exclude_unset=True).items()
        if k in ZONE_PARAMS_FIELDS
    }
    bad = _validate_zone(data)
    if bad is not None:
        return Response(bad, status=400)
    for k, v in data.items():
        setattr(z, k, v)
    z.save()
    return _serialize_zone_params(z)


@router.put("/users/{username}/zones/{pk}/params", auth=JwtAuth())
def put_zone_params(request, username: str, pk: int, payload: ZoneWriteIn):
    return _update_zone_params(request, username, pk, payload)


@router.patch("/users/{username}/zones/{pk}/params", auth=JwtAuth())
def patch_zone_params(request, username: str, pk: int, payload: ZoneWriteIn):
    return _update_zone_params(request, username, pk, payload)


# ---------------------------------------------------------------------------
# ActiveGraph admin
# ---------------------------------------------------------------------------


def _serialize_active_graph(ag: ActiveGraph) -> dict[str, Any]:
    d = model_to_dict(ag)
    d.pop("id", None)
    return d


def _get_or_create_active_graph(username: str, zone_id: int):
    user = _resolve_user(username)
    if user is None:
        return None, Response(
            {"detail": f"User '{username}' not found."},
            status=404,
        )
    z = Zone.objects.filter(pk=zone_id, user=user).first()
    if z is None:
        return None, Response({"detail": "Zone not found for this user."}, status=404)
    ag, _ = ActiveGraph.objects.get_or_create(user=user, zone=z)
    return ag, None


@router.get(
    "/users/{username}/zones/{zone_id}/active-graph",
    auth=JwtAuth(),
)
def get_active_graph(request, username: str, zone_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    ag, err = _get_or_create_active_graph(username, zone_id)
    if err is not None:
        return err
    return _serialize_active_graph(ag)


@router.patch(
    "/users/{username}/zones/{zone_id}/active-graph",
    auth=JwtAuth(),
)
def patch_active_graph(request, username: str, zone_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    ag, err = _get_or_create_active_graph(username, zone_id)
    if err is not None:
        return err
    # Body is a free-form dict of <field>_status: bool — apply directly.
    import json as _json

    try:
        body = _json.loads(request.body or b"{}")
    except ValueError:
        return Response({"detail": "Invalid JSON body."}, status=400)
    for k, v in (body or {}).items():
        if hasattr(ag, k):
            setattr(ag, k, v)
    ag.save()
    return _serialize_active_graph(ag)


# Legacy /api/active-graph/<u>/<z>/ deprecated in the REST-aligned rewrite;
# use /users/<u>/zones/<z>/active-graph (already defined above) instead.


# ---------------------------------------------------------------------------
# Alerts admin override
# ---------------------------------------------------------------------------


ALERT_FIELDS = [
    "id",
    "name",
    "type",
    "description",
    "condition",
    "condition_nbr",
    "sensor_key",
    "zone",
    "is_active",
    "last_triggered_at",
    "created_at",
    "updated_at",
    "user",
]


def _serialize_alert(a: Alert) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "type": a.type,
        "description": a.description,
        "condition": a.condition,
        "condition_nbr": (
            float(a.condition_nbr) if a.condition_nbr is not None else None
        ),
        "sensor_key": a.sensor_key,
        "zone": a.zone_id,
        "is_active": a.is_active,
        "last_triggered_at": (
            a.last_triggered_at.isoformat() if a.last_triggered_at else None
        ),
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "user": a.user_id,
    }


@router.get("/users/{username}/alerts", auth=JwtAuth())
def list_user_alerts(request, username: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user = _resolve_user(username)
    if user is None:
        return Response({"detail": f"User '{username}' not found."}, status=404)
    return [
        _serialize_alert(a) for a in Alert.objects.filter(user=user).order_by("-id")
    ]


@router.get("/admin/alerts/{pk}", auth=JwtAuth())
def get_alert(request, pk: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    try:
        a = Alert.objects.get(pk=pk)
    except Alert.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)
    return _serialize_alert(a)


@router.patch("/admin/alerts/{pk}", auth=JwtAuth())
def patch_alert(request, pk: int, payload: AlertAdminPatchIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    try:
        a = Alert.objects.get(pk=pk)
    except Alert.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)
    if payload.is_active is not None:
        a.is_active = payload.is_active
        a.save(update_fields=["is_active", "updated_at"])
    return _serialize_alert(a)


@router.delete("/admin/alerts/{pk}", auth=JwtAuth())
def delete_alert(request, pk: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    try:
        a = Alert.objects.get(pk=pk)
    except Alert.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)
    a.delete()
    return Response(None, status=204)


# ---------------------------------------------------------------------------
# Per-user activity timeline
# ---------------------------------------------------------------------------


@router.get("/users/{username}/activity", auth=JwtAuth())
def user_activity(request, username: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user = _resolve_user(username)
    if user is None:
        return Response({"detail": f"User '{username}' not found."}, status=404)
    events: list[dict[str, Any]] = []
    if user.date_joined:
        events.append(
            {
                "kind": "joined",
                "label": "Compte créé",
                "at": user.date_joined.isoformat(),
            }
        )
    if user.last_login:
        events.append(
            {
                "kind": "login",
                "label": "Dernière connexion",
                "at": user.last_login.isoformat(),
            }
        )
    if user.last_notified:
        events.append(
            {
                "kind": "notified",
                "label": "Dernière notification envoyée",
                "at": user.last_notified.isoformat(),
            }
        )
    zone_count = Zone.objects.filter(user=user).count()
    events.append(
        {"kind": "zones", "label": f"{zone_count} zone(s) actives", "at": None}
    )
    triggered = (
        Alert.objects.filter(user=user, last_triggered_at__isnull=False)
        .order_by("-last_triggered_at")
        .values("name", "last_triggered_at")[:5]
    )
    for row in triggered:
        events.append(
            {
                "kind": "alert",
                "label": f"Alerte déclenchée : {row['name']}",
                "at": row["last_triggered_at"].isoformat(),
            }
        )
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    return {"events": events}


# ---------------------------------------------------------------------------
# Per-user sensor units (preferences)
# ---------------------------------------------------------------------------


@router.get("/users/{username}/sensor-units", auth=JwtAuth())
def get_sensor_units(request, username: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    from analytics.models import UserSensorUnitPreference

    user = _resolve_user(username)
    if user is None:
        return Response({"detail": f"User '{username}' not found."}, status=404)
    return {
        row.sensor_key: row.unit
        for row in UserSensorUnitPreference.objects.filter(user=user)
    }


@router.patch("/users/{username}/sensor-units", auth=JwtAuth())
def patch_sensor_units(request, username: str):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    from analytics.models import UserSensorUnitPreference

    user = _resolve_user(username)
    if user is None:
        return Response({"detail": f"User '{username}' not found."}, status=404)
    import json as _json

    try:
        body = _json.loads(request.body or b"{}")
    except ValueError:
        return Response({"detail": "Invalid JSON body."}, status=400)
    if not isinstance(body, dict):
        return Response(
            {"detail": "Body must be a JSON object of sensor_key → unit."},
            status=400,
        )
    for sensor_key, unit in body.items():
        if not isinstance(sensor_key, str) or not isinstance(unit, str):
            return Response(
                {"detail": "All keys and values must be strings."},
                status=400,
            )
        UserSensorUnitPreference.objects.update_or_create(
            user=user,
            sensor_key=sensor_key,
            defaults={"unit": unit},
        )
    return {
        row.sensor_key: row.unit
        for row in UserSensorUnitPreference.objects.filter(user=user)
    }
