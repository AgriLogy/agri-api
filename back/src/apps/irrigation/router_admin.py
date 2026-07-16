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
from django.db.models import Count, Max
from django.db.models.functions import TruncWeek
from django.forms.models import model_to_dict
from django.utils import timezone
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from analytics.models import ActiveGraph, Alert, GraphName, SensorColor, Zone
from apps.irrigation.audit import record_audit
from apps.lorawan.chirpstack.models import LoraUplink

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
    "elevation_m",
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
    elevation_m: float | None = None


class AlertAdminPatchIn(Schema):
    is_active: bool | None = None


class AlertAdminCreateIn(Schema):
    username: str
    name: str
    type: str
    condition: str
    condition_nbr: float
    sensor_key: str = ""
    description: str = ""
    zone_id: int | None = None
    is_active: bool = True


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
        # Clients = customer accounts (non-staff, non-technician) — the same
        # population /admin/analytics calls "customers". Kept separate from
        # users_active, which counts all enabled accounts (staff included).
        "clients_total": User.objects.filter(
            is_staff=False, is_technician=False
        ).count(),
        "clients_active": User.objects.filter(
            is_staff=False, is_technician=False, is_active=True
        ).count(),
        "staff_total": User.objects.filter(is_staff=True).count(),
        "zones_total": Zone.objects.count(),
        "alerts_24h": Alert.objects.filter(last_triggered_at__gte=since).count(),
    }


@router.get("/admin/analytics", auth=JwtAuth(), summary="Admin: business analytics")
def analytics(request):
    """Aggregate business KPIs for the admin dashboard (read-only, no new table).

    "Customers" = non-staff, non-technician users, so staff/technician logins
    don't skew the commercial numbers.
    """
    guard = _require_admin(request)
    if guard is not None:
        return guard

    now = timezone.now()
    customers = User.objects.filter(is_staff=False, is_technician=False)

    # Payment status split (actif / suspended / …).
    payment_status = {
        row["payement_status"] or "unknown": row["n"]
        for row in customers.values("payement_status").annotate(n=Count("id"))
    }

    # Signups per week over the last 12 weeks.
    signups_by_week = [
        {"week": row["w"].date().isoformat(), "count": row["n"]}
        for row in (
            customers.filter(date_joined__gte=now - timezone.timedelta(weeks=12))
            .annotate(w=TruncWeek("date_joined"))
            .values("w")
            .annotate(n=Count("id"))
            .order_by("w")
        )
        if row["w"] is not None
    ]

    active_users = customers.filter(is_active=True).count()
    inactive_users = customers.filter(is_active=False).count()

    # Zones-per-customer distribution, bucketed.
    buckets = {"0": 0, "1": 0, "2-3": 0, "4+": 0}
    users_with_zones = 0
    for row in (
        Zone.objects.filter(user__is_staff=False, user__is_technician=False)
        .values("user")
        .annotate(n=Count("id"))
    ):
        users_with_zones += 1
        n = row["n"]
        if n == 1:
            buckets["1"] += 1
        elif n <= 3:
            buckets["2-3"] += 1
        else:
            buckets["4+"] += 1
    buckets["0"] = max(customers.count() - users_with_zones, 0)

    alerts_by_type = [
        {"type": row["type"], "count": row["n"]}
        for row in (
            Alert.objects.filter(created_at__gte=now - timezone.timedelta(days=30))
            .values("type")
            .annotate(n=Count("id"))
            .order_by("-n")
        )
    ]

    # LoRaWAN device fleet health summary (distinct dev_eui, stale > 24h).
    # Best-effort: the uplink table may be absent in some envs/test DBs.
    stale_cutoff = now - timezone.timedelta(hours=24)
    devices_total = 0
    devices_stale = 0
    try:
        for row in LoraUplink.objects.values("dev_eui").annotate(
            last=Max("received_at")
        ):
            devices_total += 1
            if row["last"] is None or row["last"] < stale_cutoff:
                devices_stale += 1
    except Exception:  # noqa: BLE001 — table missing / db unavailable
        devices_total = devices_stale = 0

    return {
        "payment_status": payment_status,
        "signups_by_week": signups_by_week,
        "active_users": active_users,
        "inactive_users": inactive_users,
        "zones_per_user": buckets,
        "alerts_by_type": alerts_by_type,
        "devices": {
            "total": devices_total,
            "stale": devices_stale,
            "online": max(devices_total - devices_stale, 0),
        },
    }


# ---------------------------------------------------------------------------
# Device fleet health (LoRaWAN uplinks)
# ---------------------------------------------------------------------------
@router.get(
    "/admin/devices/health", auth=JwtAuth(), summary="Admin: device fleet health"
)
def device_fleet_health(request):
    """Per-device health from LoRaWAN uplinks, grouped by ``dev_eui``.

    For each device: latest name, last-seen, battery, signal (rssi/snr) and a
    24h uplink count, plus a status (online < 24h, stale < 72h, else offline).
    Best-effort: the uplink table may be absent in some envs/test DBs, so the
    whole aggregation is guarded and degrades to an empty fleet rather than 500.
    """
    guard = _require_admin(request)
    if guard is not None:
        return guard

    now = timezone.now()
    online_cutoff = now - timezone.timedelta(hours=24)
    stale_cutoff = now - timezone.timedelta(hours=72)

    devices: list[dict[str, Any]] = []
    try:
        dev_euis = list(
            LoraUplink.objects.order_by("dev_eui")
            .values_list("dev_eui", flat=True)
            .distinct()
        )
        for eui in dev_euis:
            latest = (
                LoraUplink.objects.filter(dev_eui=eui).order_by("-received_at").first()
            )
            if latest is None:
                continue
            last_seen = latest.received_at
            if last_seen is None or last_seen < stale_cutoff:
                status = "offline"
            elif last_seen < online_cutoff:
                status = "stale"
            else:
                status = "online"
            devices.append(
                {
                    "dev_eui": eui,
                    "device_name": latest.device_name or "",
                    "last_seen": last_seen.isoformat() if last_seen else None,
                    "battery_v": latest.battery_v,
                    "rssi": latest.rssi,
                    "snr": latest.snr,
                    "uplinks_24h": LoraUplink.objects.filter(
                        dev_eui=eui, received_at__gte=online_cutoff
                    ).count(),
                    "status": status,
                }
            )
    except Exception:  # noqa: BLE001 — table missing / db unavailable
        devices = []

    summary = {
        "total": len(devices),
        "online": sum(1 for d in devices if d["status"] == "online"),
        "stale": sum(1 for d in devices if d["status"] == "stale"),
        "offline": sum(1 for d in devices if d["status"] == "offline"),
    }
    return {"devices": devices, "summary": summary}


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
    record_audit(request.auth, "zone.create", "zone", z.id, {"username": username})
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
    record_audit(request.auth, "zone.delete", "zone", pk, {"username": username})
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
# GraphName + SensorColor admin (per-user, per-zone display config)
# ---------------------------------------------------------------------------


def _serialize_config(obj) -> dict[str, Any]:
    d = model_to_dict(obj)
    d.pop("id", None)
    d.pop("user", None)
    d.pop("zone", None)
    return d


def _resolve_zone(username: str, zone_id: int):
    user = _resolve_user(username)
    if user is None:
        return (
            None,
            None,
            Response({"detail": f"User '{username}' not found."}, status=404),
        )
    z = Zone.objects.filter(pk=zone_id, user=user).first()
    if z is None:
        return (
            None,
            None,
            Response({"detail": "Zone not found for this user."}, status=404),
        )
    return user, z, None


def _patch_config(request, obj):
    """Apply a free-form {field: value} body to a config row (known fields only)."""
    import json as _json

    try:
        body = _json.loads(request.body or b"{}")
    except ValueError:
        return Response({"detail": "Invalid JSON body."}, status=400)
    for k, v in (body or {}).items():
        if hasattr(obj, k) and k not in ("id", "user", "zone"):
            setattr(obj, k, v)
    obj.save()
    return _serialize_config(obj)


@router.get("/users/{username}/zones/{zone_id}/graph-names", auth=JwtAuth())
def get_graph_names(request, username: str, zone_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user, z, err = _resolve_zone(username, zone_id)
    if err is not None:
        return err
    obj, _ = GraphName.objects.get_or_create(user=user, zone=z)
    return _serialize_config(obj)


@router.patch("/users/{username}/zones/{zone_id}/graph-names", auth=JwtAuth())
def patch_graph_names(request, username: str, zone_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user, z, err = _resolve_zone(username, zone_id)
    if err is not None:
        return err
    obj, _ = GraphName.objects.get_or_create(user=user, zone=z)
    result = _patch_config(request, obj)
    record_audit(request.auth, "graph_names.update", "graph_names", obj.id)
    return result


@router.get("/users/{username}/zones/{zone_id}/sensor-colors", auth=JwtAuth())
def get_sensor_colors(request, username: str, zone_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user, z, err = _resolve_zone(username, zone_id)
    if err is not None:
        return err
    obj, _ = SensorColor.objects.get_or_create(user=user, zone=z)
    return _serialize_config(obj)


@router.patch("/users/{username}/zones/{zone_id}/sensor-colors", auth=JwtAuth())
def patch_sensor_colors(request, username: str, zone_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user, z, err = _resolve_zone(username, zone_id)
    if err is not None:
        return err
    obj, _ = SensorColor.objects.get_or_create(user=user, zone=z)
    result = _patch_config(request, obj)
    record_audit(request.auth, "sensor_colors.update", "sensor_colors", obj.id)
    return result


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
# Global alerts console (cross-user list + distributions)
# ---------------------------------------------------------------------------


def _serialize_alert_row(a: Alert) -> dict[str, Any]:
    """Alert + the owner's username, for the cross-user console table."""
    row = _serialize_alert(a)
    row["username"] = a.user.username if a.user_id else None
    return row


@router.get("/admin/alerts", auth=JwtAuth(), summary="Admin: all alerts (filtered)")
def list_all_alerts(
    request,
    username: str | None = None,
    type: str | None = None,
    sensor_key: str | None = None,
    is_active: bool | None = None,
    zone_id: int | None = None,
):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    qs = Alert.objects.select_related("user").order_by("-id")
    if username:
        qs = qs.filter(user__username=username)
    if type:
        qs = qs.filter(type=type)
    if sensor_key:
        qs = qs.filter(sensor_key=sensor_key)
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)
    return [_serialize_alert_row(a) for a in qs[:500]]


@router.post("/admin/alerts", auth=JwtAuth(), summary="Admin: create an alert")
def create_alert(request, payload: AlertAdminCreateIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user = _resolve_user(payload.username)
    if user is None:
        return Response({"detail": f"User '{payload.username}' not found."}, status=404)
    zone = None
    if payload.zone_id is not None:
        zone = Zone.objects.filter(id=payload.zone_id, user=user).first()
        if zone is None:
            return Response(
                {"detail": f"Zone {payload.zone_id} not found for this user."},
                status=404,
            )
    alert = Alert.objects.create(
        user=user,
        zone=zone,
        name=payload.name,
        type=payload.type,
        description=payload.description or "",
        condition=payload.condition,
        condition_nbr=payload.condition_nbr,
        sensor_key=payload.sensor_key or "",
        is_active=payload.is_active,
    )
    record_audit(
        request.auth,
        "alert.create",
        "alert",
        alert.id,
        {"username": payload.username, "name": payload.name},
    )
    return _serialize_alert_row(alert)


@router.get(
    "/admin/alert-analytics",
    auth=JwtAuth(),
    summary="Admin: alert distributions",
)
def alerts_analytics(request):
    """Type/sensor distributions + recently-triggered alerts.

    NB: there is no per-alert trigger counter in the schema — ``last_triggered_at``
    is the first-ever fire (chart-overlay marker). So we report by-type / by-sensor
    counts, the triggered-ever total, and the alerts fired in the last 7 days,
    rather than a fabricated trigger frequency.
    """
    guard = _require_admin(request)
    if guard is not None:
        return guard
    now = timezone.now()
    total = Alert.objects.count()
    active = Alert.objects.filter(is_active=True).count()
    by_type = [
        {"type": row["type"], "count": row["n"]}
        for row in Alert.objects.values("type").annotate(n=Count("id")).order_by("-n")
    ]
    by_sensor = [
        {"sensor_key": row["sensor_key"], "count": row["n"]}
        for row in (
            Alert.objects.values("sensor_key")
            .annotate(n=Count("id"))
            .order_by("-n")[:15]
        )
    ]
    recently_triggered = [
        _serialize_alert_row(a)
        for a in (
            Alert.objects.filter(
                last_triggered_at__gte=now - timezone.timedelta(days=7)
            )
            .select_related("user")
            .order_by("-last_triggered_at", "-id")[:10]
        )
    ]
    return {
        "total": total,
        "active": active,
        "inactive": total - active,
        "triggered_ever": Alert.objects.filter(last_triggered_at__isnull=False).count(),
        "by_type": by_type,
        "by_sensor": by_sensor,
        "recently_triggered": recently_triggered,
    }


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
