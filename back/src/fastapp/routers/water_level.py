"""fastapp water level monitoring (agrilogy-front#261).

Endpoints from the developer brief, all owner-scoped (JWT ``user.id``);
technician (read-only) logins are refused on writes, like the other
user-owned routers.

* ``GET    /water-sources``                  the caller's sources + status
* ``POST   /water-sources``                  create (also creates its simulator)
* ``GET    /water-sources/{id}``             one source with its devices
* ``PATCH  /water-sources/{id}``             partial update (+ ``live_device``)
* ``DELETE /water-sources/{id}``             delete with devices/readings/alerts
* ``GET    /water-sources/{id}/status``      metrics, data status, open alerts
* ``GET    /water-sources/{id}/history``     ``?range=24h|7d|30d``
* ``GET    /water-sources/{id}/alerts``      alert event history
* ``POST   /water-sources/{id}/simulate``    ``{level_m}`` — SIMULATED sources only
* ``POST   /water-level/readings``           standard WaterLevelReading payload

Validation and every formula live in ``agri.core.water_level``; the router
only translates HTTP to the service layer in ``fastapp.water_level``.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal

from agri.core.water_level import (
    WaterLevelError,
    validate_calibration_table,
    validate_spec,
)
from agri.core.database import session_scope
from agri.db.water import WaterLevelAlert, WaterLevelDevice, WaterSource
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from fastapp import water_level as svc
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["water-level"])

SourceType = Literal["BASIN", "TANK", "WELL"]
Geometry = Literal["RECTANGULAR", "CYLINDRICAL", "IRREGULAR"]
SourceMode = Literal["SIMULATED", "LIVE", "CALIBRATING"]
Positive = Field(default=None, gt=0, le=10_000)


class CalibrationPoint(BaseModel):
    level_m: float = Field(ge=0)
    volume_m3: float = Field(ge=0)


class LiveDeviceIn(BaseModel):
    """Integration fields — prepared now, filled once the sensor is chosen."""

    device_id: str | None = Field(default=None, min_length=1, max_length=64)
    adapter: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None
    scale: float | None = None
    offset_m: float | None = None
    dev_eui: str | None = Field(default=None, max_length=32)
    modbus_slave_id: int | None = Field(default=None, ge=0, le=255)
    register_address: int | None = Field(default=None, ge=0, le=65535)
    register_type: str | None = Field(default=None, max_length=32)
    baud_rate: int | None = Field(default=None, gt=0)
    parity: str | None = Field(default=None, max_length=8)
    byte_order: str | None = Field(default=None, max_length=16)

    model_config = {"extra": "forbid"}


class WaterSourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_type: SourceType
    geometry: Geometry
    zone_ids: list[int] = Field(default_factory=list)
    max_height_m: float = Field(gt=0, le=1_000)
    length_m: float | None = Positive
    width_m: float | None = Positive
    diameter_m: float | None = Positive
    total_depth_m: float | None = Positive
    max_capacity_m3: float | None = Field(default=None, gt=0)
    calibration_table: list[CalibrationPoint] | None = None
    minimum_reserve_m3: float = Field(default=0, ge=0)
    low_warning_pct: float = Field(default=30, ge=0, le=100)
    critical_pct: float = Field(default=15, ge=0, le=100)
    high_pct: float = Field(default=95, ge=0, le=100)
    offline_after_min: int = Field(default=120, gt=0, le=60 * 24 * 30)
    mode: SourceMode = "SIMULATED"
    live_device: LiveDeviceIn | None = None

    model_config = {"extra": "forbid"}


class WaterSourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    source_type: SourceType | None = None
    geometry: Geometry | None = None
    zone_ids: list[int] | None = None
    max_height_m: float | None = Field(default=None, gt=0, le=1_000)
    length_m: float | None = Positive
    width_m: float | None = Positive
    diameter_m: float | None = Positive
    total_depth_m: float | None = Positive
    max_capacity_m3: float | None = Field(default=None, gt=0)
    calibration_table: list[CalibrationPoint] | None = None
    minimum_reserve_m3: float | None = Field(default=None, ge=0)
    low_warning_pct: float | None = Field(default=None, ge=0, le=100)
    critical_pct: float | None = Field(default=None, ge=0, le=100)
    high_pct: float | None = Field(default=None, ge=0, le=100)
    offline_after_min: int | None = Field(default=None, gt=0, le=60 * 24 * 30)
    mode: SourceMode | None = None
    live_device: LiveDeviceIn | None = None

    model_config = {"extra": "forbid"}


class SimulateIn(BaseModel):
    level_m: float


class ReadingIn(BaseModel):
    """The standard WaterLevelReading payload of the brief."""

    water_source_id: int | None = None
    device_id: str | None = Field(default=None, max_length=64)
    level_m: float | None = None
    raw_level: float | None = None
    measured_at: datetime.datetime | None = None
    quality: Literal["GOOD", "SUSPECT"] | None = None
    battery_percent: float | None = Field(default=None, ge=0, le=100)
    rssi: float | None = None
    raw_payload: dict[str, Any] | None = None
    mode: str | None = None  # informational: the device's adapter decides

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _error(detail: str, status: int = 400) -> DjangoStyleJSONResponse:
    return DjangoStyleJSONResponse({"detail": detail}, status_code=status)


def _read_only(user: AuthedUser) -> DjangoStyleJSONResponse | None:
    if user.is_technician:
        return _error("Read-only (technician) access.", 403)
    return None


def _not_found() -> DjangoStyleJSONResponse:
    return _error("Water source not found.", 404)


def _clean_values(values: dict[str, Any]) -> dict[str, Any]:
    if "name" in values and isinstance(values["name"], str):
        values["name"] = values["name"].strip()
    if values.get("calibration_table") is not None:
        values["calibration_table"] = validate_calibration_table(
            values["calibration_table"]
        )
    if "zone_ids" in values and values["zone_ids"] is not None:
        values["zone_ids"] = sorted({int(z) for z in values["zone_ids"]})
    return values


def _validated(values: dict[str, Any]) -> str | None:
    """Return an error message when the merged source cannot be computed."""
    if not (values.get("name") or "").strip():
        return "name is required."
    try:
        validate_spec(svc.spec_of(values))
    except WaterLevelError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@router.get("/water-sources", summary="List the caller's water sources")
def list_sources(user: AuthedUser = Depends(get_current_user)):
    now = svc.utcnow()
    with session_scope() as session:
        sources = session.scalars(
            select(WaterSource)
            .where(WaterSource.user_id == user.id)
            .order_by(WaterSource.name, WaterSource.id)
        ).all()
        return [
            {
                **svc.source_dict(s, svc.devices_of(session, s.id)),
                "status": svc.status_payload(session, s, now),
            }
            for s in sources
        ]


@router.post("/water-sources", summary="Create a water source")
def create_source(payload: WaterSourceIn, user: AuthedUser = Depends(get_current_user)):
    if (resp := _read_only(user)) is not None:
        return resp
    try:
        values = _clean_values(payload.model_dump(exclude={"live_device"}))
    except WaterLevelError as exc:
        return _error(str(exc))
    if (msg := _validated(values)) is not None:
        return _error(msg)
    now = svc.utcnow()
    with session_scope(commit=True) as session:
        source = WaterSource(user_id=user.id, created_at=now, updated_at=now, **values)
        session.add(source)
        session.flush()
        svc.create_simulator_device(session, source.id, now)
        if payload.live_device is not None:
            svc.upsert_live_device(
                session,
                source.id,
                payload.live_device.model_dump(exclude_unset=True),
                now,
            )
        body = svc.source_dict(source, svc.devices_of(session, source.id))
    return DjangoStyleJSONResponse(body, status_code=201)


@router.get("/water-sources/{source_id}", summary="One water source")
def get_source(source_id: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        source = svc.owned_source(session, source_id, user.id)
        if source is None:
            return _not_found()
        return svc.source_dict(source, svc.devices_of(session, source.id))


@router.patch("/water-sources/{source_id}", summary="Update a water source")
def update_source(
    source_id: int,
    payload: WaterSourcePatch,
    user: AuthedUser = Depends(get_current_user),
):
    if (resp := _read_only(user)) is not None:
        return resp
    try:
        changes = _clean_values(
            payload.model_dump(exclude_unset=True, exclude={"live_device"})
        )
    except WaterLevelError as exc:
        return _error(str(exc))
    not_nullable = {
        "name",
        "source_type",
        "geometry",
        "zone_ids",
        "max_height_m",
        "minimum_reserve_m3",
        "low_warning_pct",
        "critical_pct",
        "high_pct",
        "offline_after_min",
        "mode",
    }
    if nulled := sorted(k for k in not_nullable if k in changes and changes[k] is None):
        return _error(f"{', '.join(nulled)} cannot be null.")
    now = svc.utcnow()
    with session_scope(commit=True) as session:
        source = svc.owned_source(session, source_id, user.id)
        if source is None:
            return _not_found()
        merged = {f: getattr(source, f) for f in svc.SOURCE_FIELDS}
        merged.update(changes)
        if (msg := _validated(merged)) is not None:
            return _error(msg)
        for key, value in changes.items():
            setattr(source, key, value)
        source.updated_at = now
        if payload.live_device is not None:
            svc.upsert_live_device(
                session,
                source.id,
                payload.live_device.model_dump(exclude_unset=True),
                now,
            )
        session.flush()
        # New thresholds / geometry can open or close alerts on the last level.
        svc.sync_alerts(session, source, now)
        return svc.source_dict(source, svc.devices_of(session, source.id))


@router.delete("/water-sources/{source_id}", summary="Delete a water source")
def delete_source(source_id: int, user: AuthedUser = Depends(get_current_user)):
    if (resp := _read_only(user)) is not None:
        return resp
    with session_scope(commit=True) as session:
        if svc.owned_source(session, source_id, user.id) is None:
            return _not_found()
        svc.delete_source(session, source_id)
    return DjangoStyleJSONResponse(None, status_code=204)


# ---------------------------------------------------------------------------
# Status / history / alerts
# ---------------------------------------------------------------------------
@router.get("/water-sources/{source_id}/status", summary="Current level and status")
def source_status(source_id: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        source = svc.owned_source(session, source_id, user.id)
        if source is None:
            return _not_found()
        return svc.status_payload(session, source, svc.utcnow())


@router.get("/water-sources/{source_id}/history", summary="Level history")
def source_history(
    source_id: int,
    range: Literal["24h", "7d", "30d"] = Query(default="24h"),  # noqa: A002
    user: AuthedUser = Depends(get_current_user),
):
    with session_scope() as session:
        source = svc.owned_source(session, source_id, user.id)
        if source is None:
            return _not_found()
        return svc.history_payload(session, source, range, svc.utcnow())


@router.get("/water-sources/{source_id}/alerts", summary="Alert event history")
def source_alerts(
    source_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    user: AuthedUser = Depends(get_current_user),
):
    with session_scope() as session:
        if svc.owned_source(session, source_id, user.id) is None:
            return _not_found()
        rows = session.scalars(
            select(WaterLevelAlert)
            .where(WaterLevelAlert.water_source_id == source_id)
            .order_by(WaterLevelAlert.opened_at.desc(), WaterLevelAlert.id.desc())
            .limit(limit)
        ).all()
        return [svc.alert_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------
@router.post(
    "/water-sources/{source_id}/simulate", summary="Simulator: set the current level"
)
def simulate(
    source_id: int, payload: SimulateIn, user: AuthedUser = Depends(get_current_user)
):
    if (resp := _read_only(user)) is not None:
        return resp
    now = svc.utcnow()
    with session_scope(commit=True) as session:
        source = svc.owned_source(session, source_id, user.id)
        if source is None:
            return _not_found()
        if source.mode != "SIMULATED":
            return _error(
                "This water source is not in SIMULATED mode; switch its mode "
                "to use the simulator.",
                409,
            )
        device = session.scalar(
            select(WaterLevelDevice).where(
                WaterLevelDevice.water_source_id == source.id,
                WaterLevelDevice.adapter == svc.SIMULATOR_ADAPTER,
            )
        ) or svc.create_simulator_device(session, source.id, now)
        try:
            svc.ingest_reading(
                session, source, device, {"level_m": payload.level_m}, now
            )
        except WaterLevelError as exc:
            return _error(str(exc))
        body = svc.status_payload(session, source, now)
    return DjangoStyleJSONResponse(body, status_code=201)


@router.post("/water-level/readings", summary="Ingest one standard level reading")
def post_reading(payload: ReadingIn, user: AuthedUser = Depends(get_current_user)):
    if (resp := _read_only(user)) is not None:
        return resp
    if payload.level_m is None and payload.raw_level is None:
        return _error("level_m or raw_level is required.")
    if payload.water_source_id is None and payload.device_id is None:
        return _error("water_source_id or device_id is required.")
    now = svc.utcnow()
    with session_scope(commit=True) as session:
        query = select(WaterLevelDevice, WaterSource).join(
            WaterSource, WaterSource.id == WaterLevelDevice.water_source_id
        )
        query = query.where(WaterSource.user_id == user.id)
        if payload.device_id is not None:
            query = query.where(WaterLevelDevice.device_id == payload.device_id)
        if payload.water_source_id is not None:
            query = query.where(WaterSource.id == payload.water_source_id)
        rows = session.execute(query.order_by(WaterLevelDevice.id)).all()
        if not rows:
            return _error("No such device or water source.", 404)
        if payload.device_id is None:
            # Pick the device matching the source's mode.
            want_sim = rows[0].WaterSource.mode == "SIMULATED"
            rows = [
                r
                for r in rows
                if (r.WaterLevelDevice.adapter == svc.SIMULATOR_ADAPTER) == want_sim
            ] or rows
        device, source = rows[0].WaterLevelDevice, rows[0].WaterSource
        if not device.enabled:
            return _error("This device is disabled.", 409)
        is_sim = device.adapter == svc.SIMULATOR_ADAPTER
        if is_sim and source.mode != "SIMULATED":
            return _error(
                "Simulated readings are refused while the source is LIVE.", 409
            )
        body = payload.model_dump(
            exclude_none=True, exclude={"water_source_id", "device_id", "mode"}
        )
        if "measured_at" in body:
            body["measured_at"] = payload.measured_at.isoformat()
        try:
            row = svc.ingest_reading(session, source, device, body, now)
        except WaterLevelError as exc:
            return _error(str(exc))
        result = {
            "id": row.id,
            "water_source_id": source.id,
            "device_id": row.device_id,
            "mode": row.mode,
            "measured_at": row.measured_at.isoformat(),
            "level_m": row.level_m,
            "quality": row.quality,
        }
    return DjangoStyleJSONResponse(result, status_code=201)
