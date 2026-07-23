"""fastapp /sensor-calibrations — per-sensor linear correction factors.

One row per ``(device_id, sensor_key)`` holding ``value * scale_a + offset_b``
plus a display ``unit`` and a free-text ``note``. This ticket only stores and
serves the factors — APPLYING them to readings is a later phase, so nothing
here touches the reading tables.

Storage: ``analytics_sensorcalibration`` (agri-db migration ``f4b6d2e8c1a9``),
reached with parameterised raw SQL because agri-api still pins an agri-core /
agri-db that predates the table.

The migration is not applied in production, so the read degrades to the
identity calibration (``scale_a=1``, ``offset_b=0``, ``configured=false``) and
the write is refused with a 400 naming the migration — see
``fastapp.schema_compat``.

Ownership: a caller may only calibrate sensors on devices they own, resolved
through ``routers/selfreads._resolve_read_scope`` (technicians read the owner's
sensors within their granted zones and cannot write).
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text

from agri.core.database import session_scope
from agri.db.users import CustomUserCustomuser
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse
from fastapp.routers.selfreads import _ReadScope, _resolve_read_scope
from fastapp.schema_compat import (
    SENSOR_CALIBRATION_UNAVAILABLE,
    sensor_calibration_available,
)

router = APIRouter(tags=["sensor-calibration"])

_MAX_SENSOR_KEY = 64


class CalibrationIn(BaseModel):
    scale_a: float = 1.0
    offset_b: float = 0.0
    unit: str = Field(default="", max_length=32)
    is_active: bool = True
    note: str | None = Field(default=None, max_length=2000)


def _scope(session, user_id: int) -> _ReadScope:
    row = session.get(CustomUserCustomuser, user_id)
    if row is None:  # pragma: no cover - a JWT for a deleted user
        return _ReadScope(owner_id=user_id, zone_ids=None)
    return _resolve_read_scope(session, row)


def _owned_device(session, device_id: int, scope: _ReadScope):
    """The device row when the caller may see it, else None (404 material).

    ``analytics_device`` always exists, so this check works even on a schema
    without the calibration table — ownership is never skipped by the shim.
    """
    row = session.execute(
        text(
            "SELECT id, zone_id FROM analytics_device "
            "WHERE id = :device AND user_id = :owner"
        ),
        {"device": device_id, "owner": scope.owner_id},
    ).first()
    if row is None or not scope.zone_allowed(row.zone_id):
        return None
    return row


def _default(device_id: int, sensor_key: str, available: bool) -> dict[str, Any]:
    """The identity calibration: what an uncalibrated sensor reports, and what
    a deployment without the table reports for every sensor."""
    return {
        "device_id": device_id,
        "sensor_key": sensor_key,
        "scale_a": 1.0,
        "offset_b": 0.0,
        "unit": "",
        "is_active": False,
        "note": "",
        "configured": False,
        "schema_available": available,
    }


def _serialize(row, device_id: int, sensor_key: str) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "sensor_key": sensor_key,
        "scale_a": row.scale_a,
        "offset_b": row.offset_b,
        "unit": row.unit,
        "is_active": row.is_active,
        "note": row.note,
        "configured": True,
        "schema_available": True,
    }


_SELECT = (
    "SELECT id, scale_a, offset_b, unit, is_active, note "
    "FROM analytics_sensorcalibration "
    "WHERE device_id = :device AND sensor_key = :key"
)


def _reject_key(sensor_key: str) -> DjangoStyleJSONResponse | None:
    if not sensor_key or len(sensor_key) > _MAX_SENSOR_KEY:
        return DjangoStyleJSONResponse(
            {"detail": "sensor_key must be 1-64 characters."}, status_code=400
        )
    return None


@router.get(
    "/sensor-calibrations/{device_id}/{sensor_key}",
    summary="Calibration for one (device, sensor_key)",
)
def get_calibration(
    device_id: int, sensor_key: str, user: AuthedUser = Depends(get_current_user)
):
    bad_key = _reject_key(sensor_key.strip())
    if bad_key is not None:
        return bad_key
    sensor_key = sensor_key.strip()
    with session_scope() as session:
        scope = _scope(session, user.id)
        if _owned_device(session, device_id, scope) is None:
            return DjangoStyleJSONResponse(
                {"detail": "Device not found."}, status_code=404
            )
        # COMPAT SHIM (#439) — without the table nothing is calibrated, which
        # is exactly the identity calibration. The client keeps working.
        if not sensor_calibration_available(session):
            return _default(device_id, sensor_key, available=False)
        row = session.execute(
            text(_SELECT), {"device": device_id, "key": sensor_key}
        ).first()
        if row is None:
            return _default(device_id, sensor_key, available=True)
        return _serialize(row, device_id, sensor_key)


@router.put(
    "/sensor-calibrations/{device_id}/{sensor_key}",
    summary="Create or replace the calibration for one sensor",
)
def upsert_calibration(
    device_id: int,
    sensor_key: str,
    payload: CalibrationIn,
    user: AuthedUser = Depends(get_current_user),
):
    bad_key = _reject_key(sensor_key.strip())
    if bad_key is not None:
        return bad_key
    sensor_key = sensor_key.strip()
    if payload.scale_a == 0:
        return DjangoStyleJSONResponse(
            {"detail": "scale_a must not be zero."}, status_code=400
        )
    with session_scope(commit=True) as session:
        scope = _scope(session, user.id)
        if scope.is_read_only:
            return DjangoStyleJSONResponse(
                {"detail": "Read-only (technician) access."}, status_code=403
            )
        if _owned_device(session, device_id, scope) is None:
            return DjangoStyleJSONResponse(
                {"detail": "Device not found."}, status_code=404
            )
        # COMPAT SHIM (#439) — refuse loudly rather than dropping a factor the
        # farmer believes was saved, mirroring the coordinate shim's writes.
        if not sensor_calibration_available(session):
            return DjangoStyleJSONResponse(
                {"detail": SENSOR_CALIBRATION_UNAVAILABLE}, status_code=400
            )
        now = datetime.datetime.now(datetime.timezone.utc)
        params = {
            "device": device_id,
            "key": sensor_key,
            "scale_a": payload.scale_a,
            "offset_b": payload.offset_b,
            "unit": payload.unit,
            "is_active": payload.is_active,
            "note": payload.note or "",
            "now": now,
        }
        # UNIQUE (device_id, sensor_key): update in place when it is already
        # there, so repeating the same PUT is idempotent (one row, ever).
        existing = session.execute(
            text(
                "SELECT id FROM analytics_sensorcalibration "
                "WHERE device_id = :device AND sensor_key = :key"
            ),
            {"device": device_id, "key": sensor_key},
        ).first()
        if existing is None:
            session.execute(
                text(
                    "INSERT INTO analytics_sensorcalibration "
                    "(device_id, sensor_key, scale_a, offset_b, unit, is_active, "
                    " note, created_at, updated_at) "
                    "VALUES (:device, :key, :scale_a, :offset_b, :unit, :is_active, "
                    " :note, :now, :now)"
                ),
                params,
            )
            status = 201
        else:
            session.execute(
                text(
                    "UPDATE analytics_sensorcalibration SET scale_a = :scale_a, "
                    "offset_b = :offset_b, unit = :unit, is_active = :is_active, "
                    "note = :note, updated_at = :now "
                    "WHERE device_id = :device AND sensor_key = :key"
                ),
                params,
            )
            status = 200
        row = session.execute(
            text(_SELECT), {"device": device_id, "key": sensor_key}
        ).first()
        return DjangoStyleJSONResponse(
            _serialize(row, device_id, sensor_key), status_code=status
        )
