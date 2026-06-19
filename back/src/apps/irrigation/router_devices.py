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

from agriapi.api.auth import JwtAuth
from apps.irrigation.models import Device, Zone
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
