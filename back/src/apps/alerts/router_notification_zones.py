"""Custom notification-zone endpoints (django-ninja) — agrilogy-front #57.

User-owned alert groupings independent of the farm ``Zone`` rows. Each zone
holds sensor assignments (``sensor_key`` + ``source_zone``); an ``Alert`` may
bind to a notification zone instead of a farm zone (see router_alerts).

  * GET, POST            /api/notification-zones/
  * GET                  /api/notification-zones/available-sensors
  * GET, PATCH, DELETE   /api/notification-zones/<pk>
  * POST                 /api/notification-zones/<pk>/sensors
  * DELETE               /api/notification-zones/<pk>/sensors/<sensor_id>

All endpoints are scoped to the caller (``request.auth``); a technician is
blocked from writes like the other owner-facing routers.
"""

from __future__ import annotations

from typing import Any

from ninja import Router, Schema
from ninja.responses import Response

from agri.core.alerts import SENSOR_KEY_REGISTRY
from agriapi.api.auth import JwtAuth
from agriapi.api.scope import block_if_technician
from apps.alerts.engine import get_sensor_model
from analytics.models import NotificationZone, NotificationZoneSensor, Zone

router = Router()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SensorAssignmentIn(Schema):
    sensor_key: str
    source_zone: int | None = None
    label: str | None = None


class NotificationZoneIn(Schema):
    """Create/update body. All optional so PATCH works; ``sensors``, when
    provided, REPLACES the zone's assignments wholesale."""

    name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    sensors: list[SensorAssignmentIn] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_sensor(s: NotificationZoneSensor) -> dict[str, Any]:
    spec = SENSOR_KEY_REGISTRY.get(s.sensor_key, {})
    return {
        "id": s.id,
        "sensor_key": s.sensor_key,
        "source_zone": s.source_zone_id,
        "label": s.label or spec.get("label"),
        "unit": spec.get("unit"),
    }


def _serialize(nz: NotificationZone) -> dict[str, Any]:
    return {
        "id": nz.id,
        "name": nz.name,
        "description": nz.description,
        "is_active": nz.is_active,
        "user": nz.user_id,
        "created_at": nz.created_at.isoformat() if nz.created_at else None,
        "updated_at": nz.updated_at.isoformat() if nz.updated_at else None,
        "sensors": [_serialize_sensor(s) for s in nz.sensors.order_by("id")],
    }


def _owned(request, pk: int) -> NotificationZone | None:
    return NotificationZone.objects.filter(id=pk, user=request.auth).first()


def _validate_assignment(request, a: SensorAssignmentIn) -> str | None:
    if a.sensor_key not in SENSOR_KEY_REGISTRY:
        return f"Unknown sensor_key '{a.sensor_key}'."
    if (
        a.source_zone is not None
        and not Zone.objects.filter(id=a.source_zone, user=request.auth).exists()
    ):
        return f"source_zone {a.source_zone} not found or not owned by the caller."
    return None


def _replace_sensors(
    nz: NotificationZone, assignments: list[SensorAssignmentIn]
) -> None:
    nz.sensors.all().delete()
    for a in assignments:
        NotificationZoneSensor.objects.create(
            notification_zone=nz,
            sensor_key=a.sensor_key,
            source_zone_id=a.source_zone,
            label=a.label,
        )


# ---------------------------------------------------------------------------
# /api/notification-zones/
# ---------------------------------------------------------------------------


@router.get("", auth=JwtAuth(), summary="List the caller's notification zones")
def list_zones(request):
    qs = NotificationZone.objects.filter(user=request.auth).order_by("-id")
    return [_serialize(nz) for nz in qs]


@router.post("", auth=JwtAuth(), summary="Create a notification zone")
def create_zone(request, payload: NotificationZoneIn):
    blocked = block_if_technician(request)
    if blocked is not None:
        return blocked
    if not payload.name:
        return Response({"detail": "name is required."}, status=400)
    for a in payload.sensors or []:
        err = _validate_assignment(request, a)
        if err is not None:
            return Response({"detail": err}, status=400)
    nz = NotificationZone.objects.create(
        user=request.auth,
        name=payload.name,
        description=payload.description or "",
        is_active=payload.is_active if payload.is_active is not None else True,
    )
    if payload.sensors is not None:
        _replace_sensors(nz, payload.sensors)
    return Response(_serialize(nz), status=201)


# Sub-collections MUST be registered before /{pk} so the dynamic path doesn't
# shadow them.
@router.get(
    "/available-sensors",
    auth=JwtAuth(),
    summary="The caller's (farm zone, sensor_key) reading streams",
)
def available_sensors(request):
    zones = list(Zone.objects.filter(user=request.auth).values("id", "name"))
    out = []
    for z in zones:
        keys = []
        for key, spec in SENSOR_KEY_REGISTRY.items():
            try:
                model = get_sensor_model(key)
                if model.objects.filter(
                    user_id=request.auth.id, zone_id=z["id"]
                ).exists():
                    keys.append(
                        {
                            "sensor_key": key,
                            "label": spec.get("label"),
                            "unit": spec.get("unit"),
                        }
                    )
            except Exception:
                continue
        out.append({"zone_id": z["id"], "zone_name": z["name"], "sensors": keys})
    return {"zones": out}


@router.get("/{pk}", auth=JwtAuth(), summary="Get one notification zone")
def get_zone(request, pk: int):
    nz = _owned(request, pk)
    if nz is None:
        return Response({"detail": "Notification zone not found."}, status=404)
    return _serialize(nz)


@router.patch("/{pk}", auth=JwtAuth(), summary="Update a notification zone")
def update_zone(request, pk: int, payload: NotificationZoneIn):
    blocked = block_if_technician(request)
    if blocked is not None:
        return blocked
    nz = _owned(request, pk)
    if nz is None:
        return Response({"detail": "Notification zone not found."}, status=404)
    for a in payload.sensors or []:
        err = _validate_assignment(request, a)
        if err is not None:
            return Response({"detail": err}, status=400)
    data = payload.model_dump(exclude_unset=True)
    for field in ("name", "description", "is_active"):
        if field in data and data[field] is not None:
            setattr(nz, field, data[field])
    nz.save()
    if payload.sensors is not None:
        _replace_sensors(nz, payload.sensors)
    return _serialize(nz)


@router.delete("/{pk}", auth=JwtAuth(), summary="Delete a notification zone")
def delete_zone(request, pk: int):
    blocked = block_if_technician(request)
    if blocked is not None:
        return blocked
    nz = _owned(request, pk)
    if nz is None:
        return Response({"detail": "Notification zone not found."}, status=404)
    nz.sensors.all().delete()
    nz.delete()
    return Response({"deleted": pk}, status=200)


@router.post("/{pk}/sensors", auth=JwtAuth(), summary="Add a sensor assignment")
def add_sensor(request, pk: int, payload: SensorAssignmentIn):
    blocked = block_if_technician(request)
    if blocked is not None:
        return blocked
    nz = _owned(request, pk)
    if nz is None:
        return Response({"detail": "Notification zone not found."}, status=404)
    err = _validate_assignment(request, payload)
    if err is not None:
        return Response({"detail": err}, status=400)
    s = NotificationZoneSensor.objects.create(
        notification_zone=nz,
        sensor_key=payload.sensor_key,
        source_zone_id=payload.source_zone,
        label=payload.label,
    )
    return Response(_serialize_sensor(s), status=201)


@router.delete(
    "/{pk}/sensors/{sensor_id}",
    auth=JwtAuth(),
    summary="Remove a sensor assignment",
)
def remove_sensor(request, pk: int, sensor_id: int):
    blocked = block_if_technician(request)
    if blocked is not None:
        return blocked
    nz = _owned(request, pk)
    if nz is None:
        return Response({"detail": "Notification zone not found."}, status=404)
    deleted, _ = NotificationZoneSensor.objects.filter(
        id=sensor_id, notification_zone=nz
    ).delete()
    if not deleted:
        return Response({"detail": "Sensor assignment not found."}, status=404)
    return Response({"deleted": sensor_id}, status=200)
