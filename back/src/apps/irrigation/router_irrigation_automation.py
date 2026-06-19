"""Irrigation automation — scheduled programs + manual valve/pump commands.

Caller-scoped (JwtAuth); technicians are read-only and blocked from writes.
Physical actuation is simulated by default — see ``output_dispatch`` and the
``IRRIGATION_DISPATCH_ENABLED`` setting; nothing here touches hardware until
that flag is turned on and a downlink backend is wired.

The ``analytics_irrigationprogram`` / ``analytics_outputcommand`` tables
self-deploy on prod via ``scripts/ensure_irrigation_tables.py``.
"""

from __future__ import annotations

from datetime import time

from django.conf import settings
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from agriapi.api.scope import block_if_technician
from analytics.models import IrrigationProgram, OutputCommand, Zone

from .output_dispatch import dispatch_command

router = Router()


# ── schemas ──────────────────────────────────────────────────────────────────
class ProgramIn(Schema):
    name: str
    zone_id: int
    start_time: time
    weekdays: str = ""
    enabled: bool = True
    duration_min: int | None = None
    target_volume_m3: float | None = None


class CommandIn(Schema):
    zone_id: int
    action: str  # "open" | "close"
    device_id: int | None = None


# ── helpers ──────────────────────────────────────────────────────────────────
def _program(p: IrrigationProgram) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "zone_id": p.zone_id,
        "enabled": p.enabled,
        "start_time": p.start_time.isoformat() if p.start_time else None,
        "weekdays": p.weekdays,
        "duration_min": p.duration_min,
        "target_volume_m3": p.target_volume_m3,
        "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None,
    }


def _command(c: OutputCommand) -> dict:
    return {
        "id": c.id,
        "zone_id": c.zone_id,
        "device_id": c.device_id,
        "action": c.action,
        "source": c.source,
        "status": c.status,
        "detail": c.detail,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "dispatched_at": c.dispatched_at.isoformat() if c.dispatched_at else None,
    }


def _owned_zone(user, zone_id: int) -> Zone | None:
    return Zone.objects.filter(user=user, id=zone_id).first()


# ── config (so the UI can show the simulation banner) ────────────────────────
@router.get("/config", auth=JwtAuth(), summary="Irrigation automation config")
def config(request):
    return {
        "dispatch_enabled": bool(
            getattr(settings, "IRRIGATION_DISPATCH_ENABLED", False)
        )
    }


# ── programs CRUD ────────────────────────────────────────────────────────────
@router.get("/programs", auth=JwtAuth(), summary="List the caller's programs")
def list_programs(request, zone_id: int | None = None):
    qs = IrrigationProgram.objects.filter(user=request.auth).order_by("id")
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)
    return [_program(p) for p in qs]


@router.post("/programs", auth=JwtAuth(), summary="Create an irrigation program")
def create_program(request, payload: ProgramIn):
    blocked = block_if_technician(request)
    if blocked:
        return blocked
    if _owned_zone(request.auth, payload.zone_id) is None:
        return Response({"detail": "Zone not found."}, status=404)
    p = IrrigationProgram.objects.create(
        user=request.auth,
        zone_id=payload.zone_id,
        name=payload.name[:120],
        start_time=payload.start_time,
        weekdays=payload.weekdays.strip(),
        enabled=payload.enabled,
        duration_min=payload.duration_min,
        target_volume_m3=payload.target_volume_m3,
    )
    return _program(p)


@router.put("/programs/{program_id}", auth=JwtAuth(), summary="Update a program")
def update_program(request, program_id: int, payload: ProgramIn):
    blocked = block_if_technician(request)
    if blocked:
        return blocked
    p = IrrigationProgram.objects.filter(user=request.auth, id=program_id).first()
    if p is None:
        return Response({"detail": "Program not found."}, status=404)
    if _owned_zone(request.auth, payload.zone_id) is None:
        return Response({"detail": "Zone not found."}, status=404)
    p.name = payload.name[:120]
    p.zone_id = payload.zone_id
    p.start_time = payload.start_time
    p.weekdays = payload.weekdays.strip()
    p.enabled = payload.enabled
    p.duration_min = payload.duration_min
    p.target_volume_m3 = payload.target_volume_m3
    p.save()
    return _program(p)


@router.delete("/programs/{program_id}", auth=JwtAuth(), summary="Delete a program")
def delete_program(request, program_id: int):
    blocked = block_if_technician(request)
    if blocked:
        return blocked
    deleted, _ = IrrigationProgram.objects.filter(
        user=request.auth, id=program_id
    ).delete()
    if not deleted:
        return Response({"detail": "Program not found."}, status=404)
    return {"deleted": True}


# ── manual commands + history ────────────────────────────────────────────────
@router.post("/commands", auth=JwtAuth(), summary="Send a manual valve/pump command")
def send_command(request, payload: CommandIn):
    blocked = block_if_technician(request)
    if blocked:
        return blocked
    if payload.action not in (OutputCommand.OPEN, OutputCommand.CLOSE):
        return Response({"detail": "action must be 'open' or 'close'."}, status=400)
    if _owned_zone(request.auth, payload.zone_id) is None:
        return Response({"detail": "Zone not found."}, status=404)
    device_id = None
    if payload.device_id is not None:
        from apps.irrigation.models import Device

        dev = Device.objects.filter(user=request.auth, id=payload.device_id).first()
        if dev is None:
            return Response({"detail": "Device not found."}, status=404)
        device_id = dev.id
    cmd = OutputCommand.objects.create(
        user=request.auth,
        zone_id=payload.zone_id,
        device_id=device_id,
        action=payload.action,
        source=OutputCommand.MANUAL,
        status=OutputCommand.PENDING,
    )
    try:
        dispatch_command(cmd)
    except Exception as exc:  # never 500 on a dispatch problem
        cmd.status = OutputCommand.FAILED
        cmd.detail = str(exc)[:255]
        cmd.save(update_fields=["status", "detail"])
    return _command(cmd)


@router.get("/commands", auth=JwtAuth(), summary="Command history")
def list_commands(request, zone_id: int | None = None, limit: int = 50):
    qs = OutputCommand.objects.filter(user=request.auth).order_by("-id")
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)
    limit = max(1, min(limit, 200))
    return [_command(c) for c in qs[:limit]]
