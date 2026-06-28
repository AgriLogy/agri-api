"""Admin device/router registry — /devices.

CRUD over the ``Device`` table: register a hardware router/gateway/node
(dragon / lora / bivocom) by its serial / DevEUI and bind it to a user + zone
so the fleet view and health alerts can attribute uplinks. Admin-only
(``is_staff``).
"""

from __future__ import annotations

from typing import Any

from ninja import Router, Schema
from ninja.responses import Response

from agri.core.alerts import SENSOR_KEY_REGISTRY

from agriapi.api.auth import JwtAuth
from apps.irrigation.models import Device, DeviceSensor, Zone
from apps.users.models import CustomUser
from apps.users.router_admin import _require_admin

router = Router()

_VALID_TYPES = {c[0] for c in Device.DEVICE_TYPE_CHOICES}


class DeviceWriteIn(Schema):
    device_type: str | None = None
    serial: str | None = None
    name: str | None = None
    username: str | None = None  # owner
    zone_id: int | None = None
    is_active: bool | None = None
    latitude: float | None = None
    longitude: float | None = None


def _serialize(d: Device) -> dict[str, Any]:
    return {
        "id": d.id,
        "device_type": d.device_type,
        "serial": d.serial,
        "name": d.name,
        "user": d.user.username if d.user_id else None,
        "zone": d.zone_id,
        "is_active": d.is_active,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "latitude": d.latitude,
        "longitude": d.longitude,
    }


def _resolve_owner_zone(payload: DeviceWriteIn):
    """Returns (user, zone, error_response)."""
    user = None
    if payload.username is not None:
        user = CustomUser.objects.filter(username=payload.username).first()
        if user is None:
            return (
                None,
                None,
                Response({"detail": "owner username not found."}, status=400),
            )
    zone = None
    if payload.zone_id is not None:
        zone = Zone.objects.filter(id=payload.zone_id).first()
        if zone is None:
            return None, None, Response({"detail": "zone not found."}, status=400)
        if user is not None and zone.user_id != user.id:
            return (
                None,
                None,
                Response({"detail": "zone is not owned by that user."}, status=400),
            )
    return user, zone, None


@router.get("", auth=JwtAuth(), summary="Admin: list registered devices")
def list_devices(request, username: str | None = None):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    qs = Device.objects.select_related("user", "zone").order_by("-id")
    if username:
        qs = qs.filter(user__username=username)
    return [_serialize(d) for d in qs]


@router.post("", auth=JwtAuth(), summary="Admin: register a device")
def create_device(request, payload: DeviceWriteIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    if not payload.device_type or not payload.serial or not payload.username:
        return Response(
            {"detail": "device_type, serial, and username are required."},
            status=400,
        )
    if payload.device_type not in _VALID_TYPES:
        return Response(
            {"detail": f"device_type must be one of {sorted(_VALID_TYPES)}."},
            status=400,
        )
    if Device.objects.filter(serial=payload.serial).exists():
        return Response(
            {"detail": "a device with that serial already exists."}, status=400
        )
    user, zone, err = _resolve_owner_zone(payload)
    if err is not None:
        return err
    device = Device(
        device_type=payload.device_type,
        serial=payload.serial.strip(),
        name=(payload.name or "").strip(),
        user=user,
        zone=zone,
        is_active=payload.is_active if payload.is_active is not None else True,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
    device.save()
    return Response(_serialize(device), status=201)


@router.patch("/{pk}", auth=JwtAuth(), summary="Admin: update a device")
def patch_device(request, pk: int, payload: DeviceWriteIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    device = Device.objects.filter(pk=pk).first()
    if device is None:
        return Response({"detail": "device not found."}, status=404)
    if payload.device_type is not None:
        if payload.device_type not in _VALID_TYPES:
            return Response({"detail": "invalid device_type."}, status=400)
        device.device_type = payload.device_type
    if payload.serial is not None:
        if Device.objects.filter(serial=payload.serial).exclude(pk=pk).exists():
            return Response({"detail": "serial already in use."}, status=400)
        device.serial = payload.serial.strip()
    if payload.name is not None:
        device.name = payload.name.strip()
    if payload.is_active is not None:
        device.is_active = payload.is_active
    if payload.latitude is not None:
        device.latitude = payload.latitude
    if payload.longitude is not None:
        device.longitude = payload.longitude
    if payload.username is not None or payload.zone_id is not None:
        user, zone, err = _resolve_owner_zone(payload)
        if err is not None:
            return err
        if user is not None:
            device.user = user
        if payload.zone_id is not None:
            device.zone = zone
    device.save()
    return Response(_serialize(device), status=200)


@router.delete("/{pk}", auth=JwtAuth(), summary="Admin: delete a device")
def delete_device(request, pk: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    deleted, _ = Device.objects.filter(pk=pk).delete()
    if not deleted:
        return Response({"detail": "device not found."}, status=404)
    return Response({"status": "deleted"}, status=200)


# ---------------------------------------------------------------------------
# Device sensor attachments — /devices/{device_id}/sensors
#
# Each row maps a device's wire tag (e.g. a Bivocom Modbus tag) to a sensor_key
# and an optional farm zone, so an admin can declare what a router carries
# without any per-device code. The Bivocom ingest reads these to route uplinks.
# ---------------------------------------------------------------------------


class DeviceSensorWriteIn(Schema):
    tag_name: str | None = None
    sensor_key: str | None = None
    zone_id: int | None = None  # null = inherit the device's own zone at ingest
    is_active: bool | None = None


def _serialize_sensor(s: DeviceSensor) -> dict[str, Any]:
    return {
        "id": s.id,
        "device": s.device_id,
        "tag_name": s.tag_name,
        "sensor_key": s.sensor_key,
        "zone": s.zone_id,
        "is_active": s.is_active,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _resolve_sensor_zone(device: Device, zone_id: int | None):
    """Validate an optional zone override for a device sensor. Returns
    (zone, error_response). The zone must exist and belong to the device's
    owner so a router can't be wired to feed someone else's data."""
    if zone_id is None:
        return None, None
    zone = Zone.objects.filter(id=zone_id).first()
    if zone is None:
        return None, Response({"detail": "zone not found."}, status=400)
    if device.user_id and zone.user_id != device.user_id:
        return None, Response(
            {"detail": "zone is not owned by the device's owner."}, status=400
        )
    return zone, None


@router.get(
    "/{device_id}/sensors", auth=JwtAuth(), summary="Admin: list a device's sensors"
)
def list_device_sensors(request, device_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    if not Device.objects.filter(pk=device_id).exists():
        return Response({"detail": "device not found."}, status=404)
    qs = DeviceSensor.objects.filter(device_id=device_id).order_by("tag_name")
    return [_serialize_sensor(s) for s in qs]


@router.post(
    "/{device_id}/sensors", auth=JwtAuth(), summary="Admin: attach a sensor to a device"
)
def attach_device_sensor(request, device_id: int, payload: DeviceSensorWriteIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    device = Device.objects.filter(pk=device_id).first()
    if device is None:
        return Response({"detail": "device not found."}, status=404)
    tag = (payload.tag_name or "").strip()
    if not tag or not payload.sensor_key:
        return Response({"detail": "tag_name and sensor_key are required."}, status=400)
    if payload.sensor_key not in SENSOR_KEY_REGISTRY:
        return Response(
            {"detail": f"Unknown sensor_key '{payload.sensor_key}'."}, status=400
        )
    if DeviceSensor.objects.filter(device=device, tag_name=tag).exists():
        return Response(
            {"detail": f"tag_name '{tag}' is already mapped on this device."},
            status=400,
        )
    zone, err = _resolve_sensor_zone(device, payload.zone_id)
    if err is not None:
        return err
    sensor = DeviceSensor(
        device=device,
        tag_name=tag,
        sensor_key=payload.sensor_key,
        zone=zone,
        is_active=payload.is_active if payload.is_active is not None else True,
    )
    sensor.save()
    return Response(_serialize_sensor(sensor), status=201)


@router.patch(
    "/{device_id}/sensors/{sensor_id}",
    auth=JwtAuth(),
    summary="Admin: update a device sensor",
)
def patch_device_sensor(
    request, device_id: int, sensor_id: int, payload: DeviceSensorWriteIn
):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    sensor = DeviceSensor.objects.filter(pk=sensor_id, device_id=device_id).first()
    if sensor is None:
        return Response({"detail": "device sensor not found."}, status=404)
    if payload.tag_name is not None:
        tag = payload.tag_name.strip()
        if not tag:
            return Response({"detail": "tag_name cannot be empty."}, status=400)
        if (
            DeviceSensor.objects.filter(device_id=device_id, tag_name=tag)
            .exclude(pk=sensor_id)
            .exists()
        ):
            return Response(
                {"detail": f"tag_name '{tag}' is already mapped on this device."},
                status=400,
            )
        sensor.tag_name = tag
    if payload.sensor_key is not None:
        if payload.sensor_key not in SENSOR_KEY_REGISTRY:
            return Response(
                {"detail": f"Unknown sensor_key '{payload.sensor_key}'."}, status=400
            )
        sensor.sensor_key = payload.sensor_key
    if payload.zone_id is not None:
        zone, err = _resolve_sensor_zone(sensor.device, payload.zone_id)
        if err is not None:
            return err
        sensor.zone = zone
    if payload.is_active is not None:
        sensor.is_active = payload.is_active
    sensor.save()
    return Response(_serialize_sensor(sensor), status=200)


@router.delete(
    "/{device_id}/sensors/{sensor_id}",
    auth=JwtAuth(),
    summary="Admin: detach a sensor from a device",
)
def detach_device_sensor(request, device_id: int, sensor_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    deleted, _ = DeviceSensor.objects.filter(pk=sensor_id, device_id=device_id).delete()
    if not deleted:
        return Response({"detail": "device sensor not found."}, status=404)
    return Response({"status": "deleted"}, status=200)
