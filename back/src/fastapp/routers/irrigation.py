"""fastapp /irrigation — scheduled programs + manual valve/pump commands.

Strangler port of ``apps/irrigation/router_irrigation_automation.py``
(django-ninja). Caller-scoped; technicians are read-only and blocked from
writes. Byte-parity with the Django version on every response and error
envelope.

Physical actuation is simulated by default (``IRRIGATION_DISPATCH_ENABLED``);
the simulation path is inlined here (a port of ``output_dispatch.dispatch_command``).
The ``analytics_irrigationprogram`` / ``analytics_outputcommand`` tables have no
agri.db ORM model, so reads/writes are parameterised raw SQL; owned-zone /
owned-device checks hit ``analytics_zone`` / ``analytics_device`` the same way.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from agri.core.database import session_scope
from fastapp.auth import AuthedUser, get_current_user, require_level
from fastapp.json import DjangoStyleJSONResponse
from fastapp.settings import get_settings

router = APIRouter(tags=["irrigation"])


# ── schemas ──────────────────────────────────────────────────────────────────
class ProgramIn(BaseModel):
    name: str
    zone_id: int
    start_time: datetime.time
    weekdays: str = ""
    enabled: bool = True
    duration_min: int | None = None
    target_volume_m3: float | None = None


class CommandIn(BaseModel):
    zone_id: int
    action: str  # "open" | "close"
    device_id: int | None = None


# ── helpers ──────────────────────────────────────────────────────────────────
def _block_if_technician(user: AuthedUser) -> None:
    if user.is_technician:
        raise HTTPException(status_code=403, detail="Read-only (technician) access.")


def _program(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "zone_id": row.zone_id,
        "enabled": row.enabled,
        "start_time": row.start_time.isoformat() if row.start_time else None,
        "weekdays": row.weekdays,
        "duration_min": row.duration_min,
        "target_volume_m3": row.target_volume_m3,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
    }


def _command(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "zone_id": row.zone_id,
        "device_id": row.device_id,
        "action": row.action,
        "source": row.source,
        "status": row.status,
        "detail": row.detail,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "dispatched_at": (row.dispatched_at.isoformat() if row.dispatched_at else None),
    }


_PROGRAM_COLS = (
    "id, name, zone_id, enabled, start_time, weekdays, duration_min, "
    "target_volume_m3, last_run_at"
)
_COMMAND_COLS = (
    "id, zone_id, device_id, action, source, status, detail, created_at, dispatched_at"
)


def _owned_zone(session, user_id: int, zone_id: int) -> bool:
    return (
        session.execute(
            text("SELECT 1 FROM analytics_zone WHERE user_id = :u AND id = :z LIMIT 1"),
            {"u": user_id, "z": zone_id},
        ).first()
        is not None
    )


def _fetch_program(session, program_id: int):
    return session.execute(
        text(f"SELECT {_PROGRAM_COLS} FROM analytics_irrigationprogram WHERE id = :id"),
        {"id": program_id},
    ).first()


# ── config ───────────────────────────────────────────────────────────────────
@router.get("/irrigation/config", summary="Irrigation automation config")
def config(user: AuthedUser = Depends(get_current_user)):
    return {"dispatch_enabled": get_settings().irrigation_dispatch_enabled}


# ── programs CRUD ────────────────────────────────────────────────────────────
@router.get("/irrigation/programs", summary="List the caller's programs")
def list_programs(
    zone_id: int | None = None, user: AuthedUser = Depends(get_current_user)
):
    stmt = f"SELECT {_PROGRAM_COLS} FROM analytics_irrigationprogram WHERE user_id = :u"
    params: dict[str, Any] = {"u": user.id}
    if zone_id is not None:
        stmt += " AND zone_id = :z"
        params["z"] = zone_id
    stmt += " ORDER BY id"
    with session_scope() as session:
        rows = session.execute(text(stmt), params).all()
        return [_program(r) for r in rows]


@router.post("/irrigation/programs", summary="Create an irrigation program")
def create_program(
    payload: ProgramIn,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    _block_if_technician(user)
    with session_scope(commit=True) as session:
        if not _owned_zone(session, user.id, payload.zone_id):
            raise HTTPException(status_code=404, detail="Zone not found.")
        now = datetime.datetime.now(datetime.timezone.utc)
        new_id = session.execute(
            text(
                "INSERT INTO analytics_irrigationprogram "
                "(user_id, zone_id, name, start_time, weekdays, enabled, "
                "duration_min, target_volume_m3, created_at, updated_at) "
                "VALUES (:u, :z, :name, :start_time, :weekdays, :enabled, "
                ":duration_min, :target_volume_m3, :now, :now) RETURNING id"
            ),
            {
                "u": user.id,
                "z": payload.zone_id,
                "name": payload.name[:120],
                "start_time": payload.start_time,
                "weekdays": payload.weekdays.strip(),
                "enabled": payload.enabled,
                "duration_min": payload.duration_min,
                "target_volume_m3": payload.target_volume_m3,
                "now": now,
            },
        ).scalar_one()
        return _program(_fetch_program(session, new_id))


@router.put("/irrigation/programs/{program_id}", summary="Update a program")
def update_program(
    program_id: int,
    payload: ProgramIn,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    _block_if_technician(user)
    with session_scope(commit=True) as session:
        row = session.execute(
            text(
                "SELECT id FROM analytics_irrigationprogram "
                "WHERE user_id = :u AND id = :id"
            ),
            {"u": user.id, "id": program_id},
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Program not found.")
        if not _owned_zone(session, user.id, payload.zone_id):
            raise HTTPException(status_code=404, detail="Zone not found.")
        session.execute(
            text(
                "UPDATE analytics_irrigationprogram SET "
                "name = :name, zone_id = :z, start_time = :start_time, "
                "weekdays = :weekdays, enabled = :enabled, "
                "duration_min = :duration_min, target_volume_m3 = :target_volume_m3, "
                "updated_at = :now WHERE id = :id"
            ),
            {
                "name": payload.name[:120],
                "z": payload.zone_id,
                "start_time": payload.start_time,
                "weekdays": payload.weekdays.strip(),
                "enabled": payload.enabled,
                "duration_min": payload.duration_min,
                "target_volume_m3": payload.target_volume_m3,
                "now": datetime.datetime.now(datetime.timezone.utc),
                "id": program_id,
            },
        )
        return _program(_fetch_program(session, program_id))


@router.delete("/irrigation/programs/{program_id}", summary="Delete a program")
def delete_program(
    program_id: int,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("admin")),
):
    _block_if_technician(user)
    with session_scope(commit=True) as session:
        deleted = session.execute(
            text(
                "DELETE FROM analytics_irrigationprogram "
                "WHERE user_id = :u AND id = :id RETURNING id"
            ),
            {"u": user.id, "id": program_id},
        ).first()
        if deleted is None:
            raise HTTPException(status_code=404, detail="Program not found.")
        return {"deleted": True}


# ── manual commands + history ────────────────────────────────────────────────
@router.post("/irrigation/commands", summary="Send a manual valve/pump command")
def send_command(
    payload: CommandIn,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    _block_if_technician(user)
    if payload.action not in ("open", "close"):
        return DjangoStyleJSONResponse(
            {"detail": "action must be 'open' or 'close'."}, status_code=400
        )
    with session_scope(commit=True) as session:
        if not _owned_zone(session, user.id, payload.zone_id):
            raise HTTPException(status_code=404, detail="Zone not found.")
        device_id = None
        if payload.device_id is not None:
            dev = session.execute(
                text(
                    "SELECT id FROM analytics_device "
                    "WHERE user_id = :u AND id = :d LIMIT 1"
                ),
                {"u": user.id, "d": payload.device_id},
            ).first()
            if dev is None:
                raise HTTPException(status_code=404, detail="Device not found.")
            device_id = dev.id
        # Dispatch (simulated unless IRRIGATION_DISPATCH_ENABLED) — port of
        # output_dispatch.dispatch_command wrapped in the router's try/except.
        now = datetime.datetime.now(datetime.timezone.utc)
        if not get_settings().irrigation_dispatch_enabled:
            status = "simulated"
            detail = "Simulation mode — no hardware actuated."
            dispatched_at = now
        else:
            # Real actuation is intentionally unimplemented; the Django router
            # catches the raised error and records the command as failed.
            status = "failed"
            detail = (
                "IRRIGATION_DISPATCH_ENABLED is on but no downlink backend is "
                "wired. Implement the ChirpStack/Bivocom command path (and "
                "hardware-test it) before enabling physical irrigation dispatch."
            )[:255]
            dispatched_at = None
        new_id = session.execute(
            text(
                "INSERT INTO analytics_outputcommand "
                "(user_id, zone_id, device_id, action, source, status, detail, "
                "created_at, dispatched_at) "
                "VALUES (:u, :z, :d, :action, 'manual', :status, :detail, "
                ":created_at, :dispatched_at) RETURNING id"
            ),
            {
                "u": user.id,
                "z": payload.zone_id,
                "d": device_id,
                "action": payload.action,
                "status": status,
                "detail": detail,
                "created_at": now,
                "dispatched_at": dispatched_at,
            },
        ).scalar_one()
        row = session.execute(
            text(f"SELECT {_COMMAND_COLS} FROM analytics_outputcommand WHERE id = :id"),
            {"id": new_id},
        ).first()
        return _command(row)


@router.get("/irrigation/commands", summary="Command history")
def list_commands(
    zone_id: int | None = None,
    limit: int = 50,
    user: AuthedUser = Depends(get_current_user),
):
    stmt = f"SELECT {_COMMAND_COLS} FROM analytics_outputcommand WHERE user_id = :u"
    params: dict[str, Any] = {"u": user.id}
    if zone_id is not None:
        stmt += " AND zone_id = :z"
        params["z"] = zone_id
    limit = max(1, min(limit, 200))
    stmt += " ORDER BY id DESC LIMIT :lim"
    params["lim"] = limit
    with session_scope() as session:
        rows = session.execute(text(stmt), params).all()
        return [_command(r) for r in rows]
