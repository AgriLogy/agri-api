"""fastapp /devices — admin device/router registry CRUD.

Strangler port of ``apps/irrigation/router_devices.py`` (django-ninja). Admin
only (``is_staff``); byte-parity with the Django version on every response and
error envelope.

The ``analytics_device`` table has no agri.db ORM model (it is Django-managed /
unmirrored, like the technician + irrigation tables), so reads and writes go
through the agri-core SQLAlchemy session as parameterised raw SQL. The owner
username is resolved by joining ``CustomUser_customuser``.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from agri.core.database import session_scope
from fastapp.auth import LEVEL_ADMIN, AuthedUser, get_current_user, level_rank
from fastapp.json import DjangoStyleJSONResponse
from fastapp.schema_compat import (
    DEVICE_COORDINATES_UNAVAILABLE,
    device_coordinates_available,
)

router = APIRouter(tags=["devices"])

# Device.DEVICE_TYPE_CHOICES keys, verbatim (apps/irrigation/models.py).
_VALID_TYPES = {"dragon", "lora", "bivocom"}


class DeviceWriteIn(BaseModel):
    device_type: str | None = None
    serial: str | None = None
    name: str | None = None
    username: str | None = None  # owner
    zone_id: int | None = None
    is_active: bool | None = None
    # GPS position of the physical device (WGS-84 decimal degrees), so the
    # farmer map can plot each sensor at its real location.
    latitude: float | None = None
    longitude: float | None = None
    # Deprecated + ignored: history now follows the device via the device JOIN,
    # so no migration is needed. Kept so older admin clients don't 422.
    backfill: bool | None = None


class BulkAssignIn(BaseModel):
    """Attribute many existing devices to one account + zone in a single call."""

    device_ids: list[int] = []
    username: str | None = None  # target owner
    zone_id: int | None = None  # target zone (must belong to ``username``)
    backfill: bool = False  # deprecated + ignored (history follows the device)


def _require_admin(user: AuthedUser) -> None:
    """RBAC tier gate (#444): the device/router registry is an admin-only
    surface (create / update / delete / bulk-assign are destructive admin
    actions). 403 body kept verbatim (``Admin access required``); only the
    condition moved from ``is_staff`` to the ordered access-level scale."""
    if level_rank(user.access_level) < level_rank(LEVEL_ADMIN):
        raise HTTPException(status_code=403, detail="Admin access required")


def _carries_coordinates(payload: DeviceWriteIn) -> bool:
    """COMPAT SHIM (#436): does this write actually try to store a position?
    A payload that omits both fields is unaffected by the missing columns."""
    return payload.latitude is not None or payload.longitude is not None


def _serialize(row) -> dict[str, Any]:
    """Match the Django ``_serialize(Device)`` shape + field order. ``row`` is
    a joined result row with the device columns + the owner ``username``.

    ``latitude``/``longitude`` are read with ``getattr`` because the select
    omits them on a schema that lacks the columns (COMPAT SHIM #436) — the
    response keeps its shape, the coordinates are simply null."""
    return {
        "id": row.id,
        "device_type": row.device_type,
        "serial": row.serial,
        "name": row.name,
        "user": row.username if row.user_id else None,
        "zone": row.zone_id,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "latitude": getattr(row, "latitude", None),
        "longitude": getattr(row, "longitude", None),
    }


def _select_sql(has_coordinates: bool) -> str:
    """The device+owner select. COMPAT SHIM (#436): the coordinate columns are
    named only when the held agri-db migration that adds them has been applied;
    otherwise the statement would raise ``UndefinedColumn``."""
    coordinates = ", d.latitude, d.longitude" if has_coordinates else ""
    return (
        "SELECT d.id, d.device_type, d.serial, d.name, d.user_id, d.zone_id, "
        f"d.is_active, d.created_at{coordinates}, u.username "
        "FROM analytics_device d "
        'JOIN "CustomUser_customuser" u ON u.id = d.user_id'
    )


def _fetch(session, pk: int):
    stmt = _select_sql(device_coordinates_available(session)) + " WHERE d.id = :pk"
    return session.execute(text(stmt), {"pk": pk}).first()


def _resolve_owner_zone(session, payload: DeviceWriteIn):
    """Returns (user_id, zone_id, error). ``error`` is a 400 response or None.

    ``user_id`` / ``zone_id`` are only set when the corresponding field was
    supplied (else None), mirroring the Django helper."""
    user_id = None
    if payload.username is not None:
        row = session.execute(
            text('SELECT id FROM "CustomUser_customuser" WHERE username = :u'),
            {"u": payload.username},
        ).first()
        if row is None:
            return (
                None,
                None,
                DjangoStyleJSONResponse(
                    {"detail": "owner username not found."}, status_code=400
                ),
            )
        user_id = row.id
    zone_id = None
    if payload.zone_id is not None:
        row = session.execute(
            text("SELECT id, user_id FROM analytics_zone WHERE id = :z"),
            {"z": payload.zone_id},
        ).first()
        if row is None:
            return (
                None,
                None,
                DjangoStyleJSONResponse({"detail": "zone not found."}, status_code=400),
            )
        if user_id is not None and row.user_id != user_id:
            return (
                None,
                None,
                DjangoStyleJSONResponse(
                    {"detail": "zone is not owned by that user."}, status_code=400
                ),
            )
        zone_id = row.id
    return user_id, zone_id, None


@router.get("/devices", summary="Admin: list registered devices")
def list_devices(
    username: str | None = None, user: AuthedUser = Depends(get_current_user)
):
    _require_admin(user)
    params: dict[str, Any] = {}
    with session_scope() as session:
        stmt = _select_sql(device_coordinates_available(session))
        if username:
            stmt += " WHERE u.username = :username"
            params["username"] = username
        stmt += " ORDER BY d.id DESC"
        rows = session.execute(text(stmt), params).all()
        return [_serialize(r) for r in rows]


@router.post("/devices", summary="Admin: register a device")
def create_device(payload: DeviceWriteIn, user: AuthedUser = Depends(get_current_user)):
    _require_admin(user)
    if not payload.device_type or not payload.serial or not payload.username:
        return DjangoStyleJSONResponse(
            {"detail": "device_type, serial, and username are required."},
            status_code=400,
        )
    if payload.device_type not in _VALID_TYPES:
        return DjangoStyleJSONResponse(
            {"detail": f"device_type must be one of {sorted(_VALID_TYPES)}."},
            status_code=400,
        )
    with session_scope(commit=True) as session:
        # COMPAT SHIM (#436): refuse — loudly and early — to accept coordinates
        # the schema cannot store, rather than dropping them silently (the admin
        # would believe the pin was saved and the map would never show it).
        has_coordinates = device_coordinates_available(session)
        if not has_coordinates and _carries_coordinates(payload):
            return DjangoStyleJSONResponse(
                {"detail": DEVICE_COORDINATES_UNAVAILABLE}, status_code=400
            )
        exists = session.execute(
            text("SELECT 1 FROM analytics_device WHERE serial = :s LIMIT 1"),
            {"s": payload.serial},
        ).first()
        if exists is not None:
            return DjangoStyleJSONResponse(
                {"detail": "a device with that serial already exists."},
                status_code=400,
            )
        user_id, zone_id, err = _resolve_owner_zone(session, payload)
        if err is not None:
            return err
        values: dict[str, Any] = {
            "user_id": user_id,
            "zone_id": zone_id,
            "device_type": payload.device_type,
            "serial": payload.serial.strip(),
            "name": (payload.name or "").strip(),
            "is_active": payload.is_active if payload.is_active is not None else True,
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        }
        if has_coordinates:  # COMPAT SHIM (#436) — else the columns don't exist
            values["latitude"] = payload.latitude
            values["longitude"] = payload.longitude
        columns = ", ".join(values)
        placeholders = ", ".join(f":{col}" for col in values)
        new_id = session.execute(
            text(
                f"INSERT INTO analytics_device ({columns}) "
                f"VALUES ({placeholders}) RETURNING id"
            ),
            values,
        ).scalar_one()
        return DjangoStyleJSONResponse(
            _serialize(_fetch(session, new_id)), status_code=201
        )


@router.post(
    "/devices/bulk-assign",
    summary="Admin: attribute many devices to one account/zone",
)
def bulk_assign_devices(
    payload: BulkAssignIn, user: AuthedUser = Depends(get_current_user)
):
    _require_admin(user)
    if not payload.device_ids:
        return DjangoStyleJSONResponse(
            {"detail": "device_ids is required."}, status_code=400
        )
    if not payload.username or payload.zone_id is None:
        return DjangoStyleJSONResponse(
            {"detail": "username and zone_id are required."}, status_code=400
        )
    with session_scope(commit=True) as session:
        # Reuse the single-device owner/zone validation (owner exists + zone is
        # owned by that owner).
        user_id, zone_id, err = _resolve_owner_zone(
            session,
            DeviceWriteIn(username=payload.username, zone_id=payload.zone_id),
        )
        if err is not None:
            return err
        assigned: list[int] = []
        failed: list[dict[str, Any]] = []
        # Just re-point each device at the new owner/zone. Readings resolve
        # ownership via the device JOIN, so the device's whole history follows
        # this update instantly — no backfill / reading rewrite.
        for device_id in payload.device_ids:
            updated = session.execute(
                text(
                    "UPDATE analytics_device SET user_id = :u, zone_id = :z "
                    "WHERE id = :id RETURNING id"
                ),
                {"u": user_id, "z": zone_id, "id": device_id},
            ).first()
            if updated is None:
                failed.append({"id": device_id, "reason": "device not found."})
            else:
                assigned.append(device_id)
    return DjangoStyleJSONResponse({"assigned": assigned, "failed": failed})


@router.patch("/devices/{pk}", summary="Admin: update a device")
def patch_device(
    pk: int, payload: DeviceWriteIn, user: AuthedUser = Depends(get_current_user)
):
    _require_admin(user)
    with session_scope(commit=True) as session:
        # COMPAT SHIM (#436) — same contract as create: coordinates the schema
        # cannot store are rejected, never silently discarded.
        if not device_coordinates_available(session) and _carries_coordinates(payload):
            return DjangoStyleJSONResponse(
                {"detail": DEVICE_COORDINATES_UNAVAILABLE}, status_code=400
            )
        device = _fetch(session, pk)
        if device is None:
            raise HTTPException(status_code=404, detail="device not found.")
        updates: dict[str, Any] = {}
        if payload.device_type is not None:
            if payload.device_type not in _VALID_TYPES:
                return DjangoStyleJSONResponse(
                    {"detail": "invalid device_type."}, status_code=400
                )
            updates["device_type"] = payload.device_type
        if payload.serial is not None:
            clash = session.execute(
                text(
                    "SELECT 1 FROM analytics_device "
                    "WHERE serial = :s AND id <> :pk LIMIT 1"
                ),
                {"s": payload.serial, "pk": pk},
            ).first()
            if clash is not None:
                return DjangoStyleJSONResponse(
                    {"detail": "serial already in use."}, status_code=400
                )
            updates["serial"] = payload.serial.strip()
        if payload.name is not None:
            updates["name"] = payload.name.strip()
        if payload.is_active is not None:
            updates["is_active"] = payload.is_active
        if payload.latitude is not None:
            updates["latitude"] = payload.latitude
        if payload.longitude is not None:
            updates["longitude"] = payload.longitude
        if payload.username is not None or payload.zone_id is not None:
            user_id, zone_id, err = _resolve_owner_zone(session, payload)
            if err is not None:
                return err
            if user_id is not None:
                updates["user_id"] = user_id
            if payload.zone_id is not None:
                updates["zone_id"] = zone_id
        if updates:
            set_clause = ", ".join(f"{col} = :{col}" for col in updates)
            session.execute(
                text(f"UPDATE analytics_device SET {set_clause} WHERE id = :pk"),
                {**updates, "pk": pk},
            )
        # An owner/zone change needs no data migration: readings resolve
        # ownership via the device JOIN, so the device's history follows this
        # update automatically.
        return DjangoStyleJSONResponse(_serialize(_fetch(session, pk)))


@router.delete("/devices/{pk}", summary="Admin: delete a device")
def delete_device(pk: int, user: AuthedUser = Depends(get_current_user)):
    _require_admin(user)
    with session_scope(commit=True) as session:
        deleted = session.execute(
            text("DELETE FROM analytics_device WHERE id = :pk RETURNING id"),
            {"pk": pk},
        ).first()
        if deleted is None:
            raise HTTPException(status_code=404, detail="device not found.")
        return DjangoStyleJSONResponse({"status": "deleted"})
