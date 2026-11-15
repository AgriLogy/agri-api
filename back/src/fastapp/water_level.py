"""Water level monitoring — service layer (agrilogy-front#261).

The routers in ``fastapp.routers.water_level`` parse requests and call these
functions; the formulas themselves (volume, fill %, data status, which alerts
should be open) live in ``agri.core.water_level`` so the simulator today and a
live sensor tomorrow go through exactly the same code.

Alerts: after every reading, and on the beat sweep for sources that stopped
reporting, :func:`sync_alerts` opens the alerts that should be open and
resolves the ones that no longer apply. ``water_level_alert`` keeps at most one
open row per (source, kind), so a level that stays low raises one alert, not
one per reading. A newly opened alert enqueues one email to the owner.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import asdict
from typing import Any

from agri.core.water_level import (
    DeviceConfig,
    WaterLevelError,
    WaterLevelReading,
    WaterSourceSpec,
    active_alert_kinds,
    compute_metrics,
    data_status,
    get_adapter,
)
from agri.db.water import (
    WaterLevelAlert,
    WaterLevelDevice,
    WaterLevelReading as ReadingRow,
    WaterSource,
)
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from fastapp import celery

log = logging.getLogger(__name__)

SIMULATOR_ADAPTER = "simulator"
LEVEL_ALERT_KINDS = ("LOW", "CRITICAL", "HIGH")
# Level alerts are only re-evaluated from a usable current level.
_LEVEL_STATUSES = ("LIVE", "SIMULATED", "STALE")

HISTORY_RANGES = {
    "24h": datetime.timedelta(hours=24),
    "7d": datetime.timedelta(days=7),
    "30d": datetime.timedelta(days=30),
}
HISTORY_MAX_POINTS = 300

SEND_ALERT_EMAIL_TASK = "agriapi.tasks.send_water_level_alert_email"

SOURCE_FIELDS = (
    "name",
    "source_type",
    "geometry",
    "zone_ids",
    "max_height_m",
    "length_m",
    "width_m",
    "diameter_m",
    "total_depth_m",
    "max_capacity_m3",
    "calibration_table",
    "minimum_reserve_m3",
    "low_warning_pct",
    "critical_pct",
    "high_pct",
    "offline_after_min",
    "mode",
)
DEVICE_FIELDS = (
    "device_id",
    "adapter",
    "enabled",
    "scale",
    "offset_m",
    "dev_eui",
    "modbus_slave_id",
    "register_address",
    "register_type",
    "baud_rate",
    "parity",
    "byte_order",
)


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Row <-> domain
# ---------------------------------------------------------------------------
def spec_of(source: WaterSource | dict[str, Any]) -> WaterSourceSpec:
    get = source.get if isinstance(source, dict) else source.__getattribute__
    return WaterSourceSpec(
        source_type=get("source_type"),
        geometry=get("geometry"),
        max_height_m=get("max_height_m"),
        length_m=get("length_m"),
        width_m=get("width_m"),
        diameter_m=get("diameter_m"),
        total_depth_m=get("total_depth_m"),
        max_capacity_m3=get("max_capacity_m3"),
        calibration_table=get("calibration_table"),
        minimum_reserve_m3=get("minimum_reserve_m3") or 0.0,
        low_warning_pct=get("low_warning_pct"),
        critical_pct=get("critical_pct"),
        high_pct=get("high_pct"),
        offline_after_min=get("offline_after_min"),
        mode=get("mode"),
    )


def reading_of(row: ReadingRow) -> WaterLevelReading:
    return WaterLevelReading(
        level_m=row.level_m,
        measured_at=row.measured_at,
        device_id=row.device_id,
        mode=row.mode,
        quality=row.quality,
        battery_percent=row.battery_percent,
        rssi=row.rssi,
    )


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def source_dict(source: WaterSource, devices: list[WaterLevelDevice]) -> dict:
    out: dict[str, Any] = {"id": source.id}
    out.update({f: getattr(source, f) for f in SOURCE_FIELDS})
    out["zone_ids"] = list(source.zone_ids or [])
    out["capacity_m3"] = compute_metrics(spec_of(source), 0).capacity_m3
    out["created_at"] = _iso(source.created_at)
    out["updated_at"] = _iso(source.updated_at)
    out["devices"] = [device_dict(d) for d in devices]
    return out


def device_dict(device: WaterLevelDevice) -> dict:
    out = {"id": device.id}
    out.update({f: getattr(device, f) for f in DEVICE_FIELDS})
    return out


def reading_dict(row: ReadingRow | None) -> dict | None:
    if row is None:
        return None
    return {
        "measured_at": _iso(row.measured_at),
        "level_m": row.level_m,
        "mode": row.mode,
        "quality": row.quality,
        "device_id": row.device_id,
        "battery_percent": row.battery_percent,
        "rssi": row.rssi,
    }


def alert_dict(row: WaterLevelAlert) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "opened_at": _iso(row.opened_at),
        "resolved_at": _iso(row.resolved_at),
        "level_m": row.level_m,
        "fill_percent": row.fill_percent,
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def owned_source(session: Session, source_id: int, user_id: int) -> WaterSource | None:
    return session.scalar(
        select(WaterSource).where(
            WaterSource.id == source_id, WaterSource.user_id == user_id
        )
    )


def devices_of(session: Session, source_id: int) -> list[WaterLevelDevice]:
    return list(
        session.scalars(
            select(WaterLevelDevice)
            .where(WaterLevelDevice.water_source_id == source_id)
            .order_by(WaterLevelDevice.id)
        )
    )


def last_reading(session: Session, source_id: int) -> ReadingRow | None:
    return session.scalar(
        select(ReadingRow)
        .where(ReadingRow.water_source_id == source_id)
        .order_by(ReadingRow.measured_at.desc(), ReadingRow.id.desc())
        .limit(1)
    )


def open_alerts(session: Session, source_id: int) -> list[WaterLevelAlert]:
    return list(
        session.scalars(
            select(WaterLevelAlert)
            .where(
                WaterLevelAlert.water_source_id == source_id,
                WaterLevelAlert.resolved_at.is_(None),
            )
            .order_by(WaterLevelAlert.opened_at)
        )
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def create_simulator_device(
    session: Session, source_id: int, now: datetime.datetime
) -> WaterLevelDevice:
    device = WaterLevelDevice(
        water_source_id=source_id,
        device_id=f"simulator-ws{source_id}",
        adapter=SIMULATOR_ADAPTER,
        enabled=True,
        scale=1.0,
        offset_m=0.0,
        created_at=now,
    )
    session.add(device)
    session.flush()
    return device


def upsert_live_device(
    session: Session, source_id: int, values: dict[str, Any], now: datetime.datetime
) -> WaterLevelDevice:
    """The source's non-simulator device (one per source), created on demand."""
    device = session.scalar(
        select(WaterLevelDevice).where(
            WaterLevelDevice.water_source_id == source_id,
            WaterLevelDevice.adapter != SIMULATOR_ADAPTER,
        )
    )
    if device is None:
        device = WaterLevelDevice(
            water_source_id=source_id,
            device_id=values.get("device_id") or f"device-ws{source_id}",
            adapter=values.get("adapter") or "manual",
            created_at=now,
        )
        session.add(device)
    for key, value in values.items():
        if key in DEVICE_FIELDS and (value is not None or key not in ("device_id",)):
            setattr(device, key, value)
    session.flush()
    return device


def ingest_reading(
    session: Session,
    source: WaterSource,
    device: WaterLevelDevice,
    payload: dict[str, Any],
    now: datetime.datetime,
) -> ReadingRow:
    """Normalise ``payload`` through the device's adapter, store it, sync alerts.

    Raises :class:`WaterLevelError` for an unusable payload.
    """
    adapter = get_adapter(device.adapter)
    reading = adapter.normalize(
        payload,
        DeviceConfig(
            device_id=device.device_id, scale=device.scale, offset_m=device.offset_m
        ),
        spec_of(source),
        now=now,
    )
    if reading.measured_at > now + datetime.timedelta(minutes=5):
        raise WaterLevelError("measured_at is in the future")
    quality = payload.get("quality") if reading.quality == "GOOD" else None
    row = ReadingRow(
        water_source_id=source.id,
        device_id=reading.device_id,
        mode=reading.mode,
        measured_at=reading.measured_at,
        received_at=now,
        level_m=reading.level_m,
        quality=quality if quality in ("GOOD", "SUSPECT") else reading.quality,
        battery_percent=reading.battery_percent,
        rssi=reading.rssi,
        raw_payload=dict(reading.raw_payload) if reading.raw_payload else None,
    )
    session.add(row)
    session.flush()
    sync_alerts(session, source, now)
    return row


def sync_alerts(
    session: Session, source: WaterSource, now: datetime.datetime
) -> list[int]:
    """Open/resolve this source's alerts from its latest reading.

    OFFLINE follows the data status. Level alerts (LOW / CRITICAL / HIGH) are
    only re-evaluated from a usable level — an offline, invalid or
    calibrating source keeps whatever level alert it had. Returns the ids of
    newly opened alerts (an email is enqueued for each).
    """
    spec = spec_of(source)
    last = last_reading(session, source.id)
    reading = reading_of(last) if last is not None else None
    status = data_status(spec, reading, now)
    metrics = compute_metrics(spec, last.level_m) if last is not None else None
    fill = metrics.fill_percent if metrics is not None else None
    active = active_alert_kinds(spec, fill, status)

    managed = {"OFFLINE"}
    if status in _LEVEL_STATUSES:
        managed |= set(LEVEL_ALERT_KINDS)

    resolve = [k for k in managed if k not in active]
    if resolve:
        session.execute(
            update(WaterLevelAlert)
            .where(
                WaterLevelAlert.water_source_id == source.id,
                WaterLevelAlert.resolved_at.is_(None),
                WaterLevelAlert.kind.in_(resolve),
            )
            .values(resolved_at=now)
        )

    opened: list[int] = []
    for kind in sorted(active):
        new_id = session.scalar(
            pg_insert(WaterLevelAlert)
            .values(
                water_source_id=source.id,
                user_id=source.user_id,
                kind=kind,
                opened_at=now,
                level_m=last.level_m if last is not None else None,
                fill_percent=fill,
            )
            .on_conflict_do_nothing(
                index_elements=["water_source_id", "kind"],
                index_where=text("resolved_at IS NULL"),
            )
            .returning(WaterLevelAlert.id)
        )
        if new_id is not None:
            opened.append(new_id)
    session.flush()
    for alert_id in opened:
        try:
            celery.send_task(SEND_ALERT_EMAIL_TASK, alert_id=alert_id)
        except Exception as exc:  # broker down must not fail the reading
            log.warning("water level alert %s: enqueue failed: %s", alert_id, exc)
    return opened


def delete_source(session: Session, source_id: int) -> None:
    for table in (WaterLevelAlert, ReadingRow, WaterLevelDevice):
        session.query(table).filter(table.water_source_id == source_id).delete()
    session.query(WaterSource).filter(WaterSource.id == source_id).delete()


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------
def status_payload(
    session: Session, source: WaterSource, now: datetime.datetime
) -> dict[str, Any]:
    spec = spec_of(source)
    last = last_reading(session, source.id)
    status = data_status(spec, reading_of(last) if last else None, now)
    metrics = compute_metrics(spec, last.level_m) if last is not None else None

    trend_m3 = trend_level_m = None
    if last is not None:
        first = session.scalar(
            select(ReadingRow)
            .where(
                ReadingRow.water_source_id == source.id,
                ReadingRow.measured_at >= now - HISTORY_RANGES["24h"],
                ReadingRow.quality != "INVALID",
            )
            .order_by(ReadingRow.measured_at, ReadingRow.id)
            .limit(1)
        )
        if first is not None and first.id != last.id:
            trend_level_m = last.level_m - first.level_m
            start = compute_metrics(spec, first.level_m).volume_m3
            if (
                metrics is not None
                and metrics.volume_m3 is not None
                and start is not None
            ):
                trend_m3 = metrics.volume_m3 - start

    return {
        "water_source_id": source.id,
        "name": source.name,
        "source_type": source.source_type,
        "mode": source.mode,
        "data_status": status,
        "metrics": asdict(metrics) if metrics is not None else None,
        "last_reading": reading_dict(last),
        "trend_24h_m3": trend_m3,
        "trend_24h_level_m": trend_level_m,
        "thresholds": {
            "low_warning_pct": source.low_warning_pct,
            "critical_pct": source.critical_pct,
            "high_pct": source.high_pct,
            "offline_after_min": source.offline_after_min,
        },
        "open_alerts": [alert_dict(a) for a in open_alerts(session, source.id)],
        "server_time": _iso(now),
    }


def history_payload(
    session: Session, source: WaterSource, range_key: str, now: datetime.datetime
) -> dict[str, Any]:
    """Readings over the range, bucket-averaged to at most HISTORY_MAX_POINTS."""
    window = HISTORY_RANGES[range_key]
    start = now - window
    rows = session.execute(
        select(ReadingRow.measured_at, ReadingRow.level_m, ReadingRow.mode)
        .where(
            ReadingRow.water_source_id == source.id,
            ReadingRow.measured_at >= start,
            ReadingRow.measured_at <= now,
            ReadingRow.quality != "INVALID",
        )
        .order_by(ReadingRow.measured_at)
    ).all()

    if len(rows) > HISTORY_MAX_POINTS:
        bucket = window / HISTORY_MAX_POINTS
        grouped: dict[int, list] = {}
        for r in rows:
            grouped.setdefault(int((r.measured_at - start) / bucket), []).append(r)
        points_src = [
            (
                start + bucket * idx + bucket / 2,
                sum(r.level_m for r in group) / len(group),
                group[-1].mode,
            )
            for idx, group in sorted(grouped.items())
        ]
    else:
        points_src = [(r.measured_at, r.level_m, r.mode) for r in rows]

    spec = spec_of(source)
    points = []
    for measured_at, level, mode in points_src:
        m = compute_metrics(spec, level)
        points.append(
            {
                "measured_at": _iso(measured_at),
                "level_m": m.level_m,
                "fill_percent": m.fill_percent,
                "volume_m3": m.volume_m3,
                "mode": mode,
            }
        )
    return {
        "water_source_id": source.id,
        "range": range_key,
        "start": _iso(start),
        "end": _iso(now),
        "points": points,
    }
