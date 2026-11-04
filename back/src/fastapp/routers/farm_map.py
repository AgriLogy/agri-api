"""fastapp farm map — named sensor installations, map preferences, live status.

Phase 1 of the farm-map upgrade (AgriLogy/agri-api#459). Until now the map's
placed sensors and display settings lived in agri-web's ``localStorage``
(per-browser, lost on a cache clear); these endpoints make them server-owned:

* ``/sensor-installations`` — CRUD for a named sensor point (type, position,
  sector, installation details, and the reading stream it reports:
  ``zone_id`` + ``sensor_key``). ``client_ref`` makes the one-time import of
  browser-stored sensors idempotent: posting a ``client_ref`` that already
  exists returns the existing row (200) instead of a duplicate.
* ``/map-preferences`` — the caller's basemap, layer visibility/opacity and
  last camera.
* ``/farm-map/status`` — per installation: latest value + unit + timestamp,
  battery / signal, active alerts, and a status for the map colour
  (normal / warning / critical / offline / no_data / unlinked); per sector: the
  worst status of its sensors. Missing data is reported as such, never as a
  value.

Owner-scoped (JWT ``user.id``); writes need the ``editor`` tier and are refused
to technician (read-only) logins, like ``/sectors``. Tables come from agri-db
``c9e1a2b3d4f5`` via the Core mirrors in ``fastapp.farm_map_tables``; before
that migration, reads return empty and writes answer 503.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Literal

from agri.core.alerts import (
    SENSOR_KEY_REGISTRY,
    _latest_reading_for,
    recent_triggers_for_user,
)
from agri.core.database import session_scope
from agri.db.analytics import AnalyticsSector, AnalyticsZone
from agri.db.devices import AnalyticsDevice
from agri.db.users import CustomUserCustomuser
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from fastapp.auth import AuthedUser, get_current_user, require_level
from fastapp.farm_map_tables import map_preference, sensor_installation
from fastapp.json import DjangoStyleJSONResponse
from fastapp.schema_compat import FARM_MAP_UNAVAILABLE, farm_map_available

log = logging.getLogger(__name__)

router = APIRouter(tags=["farm-map"])

MaintenanceStatus = Literal["ok", "needs_check", "in_maintenance", "retired"]
Basemap = Literal["satellite_labels", "satellite", "streets"]
Status = Literal["normal", "warning", "critical", "offline", "no_data", "unlinked"]

#: A reading older than this is "stale" (warning); older than OFFLINE_AFTER the
#: sensor is considered offline. Sensor cadences range from 15 min (LoRa) to
#: hourly, so 6 h of silence is already abnormal.
STALE_AFTER = dt.timedelta(hours=6)
OFFLINE_AFTER = dt.timedelta(hours=24)

#: Worst-first ordering used to roll sensor statuses up to their sector.
_SEVERITY: dict[str, int] = {
    "critical": 5,
    "offline": 4,
    "warning": 3,
    "normal": 2,
    "no_data": 1,
    "unlinked": 0,
}

_FIELDS = (
    "name",
    "sensor_type",
    "sensor_key",
    "latitude",
    "longitude",
    "sector_id",
    "zone_id",
    "device_id",
    "installation_depth_cm",
    "model",
    "serial_number",
    "node_id",
    "installation_date",
    "calibration_profile",
    "notes",
    "maintenance_status",
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class _InstallationBase(BaseModel):
    sensor_key: str | None = Field(default=None, max_length=64)
    sector_id: int | None = None
    zone_id: int | None = None
    device_id: int | None = None
    installation_depth_cm: float | None = Field(default=None, ge=0, le=1000)
    model: str | None = Field(default=None, max_length=100)
    serial_number: str | None = Field(default=None, max_length=128)
    node_id: str | None = Field(default=None, max_length=128)
    installation_date: dt.date | None = None
    calibration_profile: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    maintenance_status: MaintenanceStatus | None = None

    model_config = {"extra": "forbid"}

    @field_validator("sensor_key")
    @classmethod
    def _known_key(cls, v: str | None) -> str | None:
        if v is not None and v not in SENSOR_KEY_REGISTRY:
            raise ValueError(f"Unknown sensor_key {v!r}.")
        return v


class InstallationIn(_InstallationBase):
    name: str = Field(min_length=1, max_length=100)
    sensor_type: str = Field(min_length=1, max_length=40)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    client_ref: str | None = Field(default=None, max_length=64)


class InstallationPatch(_InstallationBase):
    """Partial update: only fields present in the body are written."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    sensor_type: str | None = Field(default=None, min_length=1, max_length=40)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class LayerPref(BaseModel):
    visible: bool = True
    opacity: float = Field(default=1.0, ge=0, le=1)

    model_config = {"extra": "forbid"}


class CameraPref(BaseModel):
    center: tuple[float, float]
    zoom: float = Field(ge=0, le=24)
    bearing: float = 0.0
    pitch: float = Field(default=0.0, ge=0, le=85)

    model_config = {"extra": "forbid"}

    @field_validator("center")
    @classmethod
    def _valid_center(cls, v: tuple[float, float]) -> tuple[float, float]:
        lon, lat = v
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("center must be [lon, lat] in range.")
        return v


class MapPreferencesIn(BaseModel):
    basemap: Basemap | None = None
    layers: dict[str, LayerPref] | None = Field(default=None, max_length=40)
    camera: CameraPref | None = None

    model_config = {"extra": "forbid"}

    @field_validator("layers")
    @classmethod
    def _layer_ids(cls, v: dict[str, LayerPref] | None):
        if v is not None and any(not k or len(k) > 40 for k in v):
            raise ValueError("Layer ids must be 1-40 characters.")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _json(detail: str, status_code: int) -> DjangoStyleJSONResponse:
    return DjangoStyleJSONResponse({"detail": detail}, status_code=status_code)


def _block_if_technician(session: Session, user_id: int):
    row = session.get(CustomUserCustomuser, user_id)
    if row is not None and getattr(row, "is_technician", False):
        return _json("Read-only (technician) access.", 403)
    return None


def _check_refs(session: Session, user_id: int, values: dict[str, Any]):
    """Every referenced sector / zone / device must belong to the caller.
    Returns a 400 response naming the bad reference, else ``None``."""
    checks = (
        ("sector_id", AnalyticsSector),
        ("zone_id", AnalyticsZone),
        ("device_id", AnalyticsDevice),
    )
    for field, model in checks:
        ref = values.get(field)
        if ref is None:
            continue
        owner = session.scalar(select(model.user_id).where(model.id == ref))
        if owner != user_id:
            return _json(f"{field} {ref} not found.", 400)
    return None


def _serialize(row: Any) -> dict[str, Any]:
    m = dict(row._mapping)
    out = {"id": m["id"], **{k: m.get(k) for k in _FIELDS}}
    if out["installation_date"] is not None:
        out["installation_date"] = out["installation_date"].isoformat()
    out["client_ref"] = m.get("client_ref")
    for key in ("created_at", "updated_at"):
        out[key] = m[key].isoformat() if m.get(key) else None
    return out


def _owned_row(session: Session, pk: int, user_id: int):
    return session.execute(
        select(sensor_installation).where(
            sensor_installation.c.id == pk,
            sensor_installation.c.user_id == user_id,
        )
    ).first()


def classify_status(
    timestamp: dt.datetime | None,
    *,
    now: dt.datetime,
    triggered: bool = False,
    linked: bool = True,
) -> Status:
    """Map colour for one sensor from its newest reading.

    ``unlinked`` (no reading stream configured) and ``no_data`` (never
    reported) are grey; a reading older than ``OFFLINE_AFTER`` is ``offline``
    even if an alert is triggered, because the alert is evaluating a stale
    value. Otherwise a triggered alert wins over freshness.
    """
    if not linked:
        return "unlinked"
    if timestamp is None:
        return "no_data"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
    age = now - timestamp
    if age > OFFLINE_AFTER:
        return "offline"
    if triggered:
        return "critical"
    if age > STALE_AFTER:
        return "warning"
    return "normal"


def worst_status(statuses: list[str]) -> Status:
    """Sector roll-up: the most severe status among its sensors."""
    if not statuses:
        return "no_data"
    return max(statuses, key=lambda s: _SEVERITY.get(s, 0))  # type: ignore[return-value]


def _reading(session: Session, key: str, zone_id: int | None, user_id: int, cache):
    """Latest (value, timestamp) for a stream, memoised per request."""
    ck = (key, zone_id)
    if ck not in cache:
        latest = _latest_reading_for(session, key, zone_id=zone_id, user_id=user_id)
        cache[ck] = (latest.value, latest.timestamp)
    return cache[ck]


def _reading_json(value: Any, ts: dt.datetime | None, key: str) -> dict | None:
    if ts is None:
        return None
    return {
        "value": float(value) if value is not None else None,
        "unit": SENSOR_KEY_REGISTRY.get(key, {}).get("unit"),
        "timestamp": ts.isoformat(),
    }


# ---------------------------------------------------------------------------
# Sensor installations
# ---------------------------------------------------------------------------
@router.get("/sensor-installations", summary="Caller's sensors placed on the map")
def list_installations(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        if not farm_map_available(session):
            return []
        rows = session.execute(
            select(sensor_installation)
            .where(sensor_installation.c.user_id == user.id)
            .order_by(sensor_installation.c.name, sensor_installation.c.id)
        ).all()
        return [_serialize(r) for r in rows]


@router.post("/sensor-installations", summary="Place a named sensor on the map")
def create_installation(
    payload: InstallationIn,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    with session_scope(commit=True) as session:
        guard = _block_if_technician(session, user.id)
        if guard is not None:
            return guard
        if not farm_map_available(session):
            return _json(FARM_MAP_UNAVAILABLE, 503)
        if payload.client_ref:
            existing = session.execute(
                select(sensor_installation).where(
                    sensor_installation.c.user_id == user.id,
                    sensor_installation.c.client_ref == payload.client_ref,
                )
            ).first()
            if existing is not None:
                return _serialize(existing)
        values = payload.model_dump()
        values["name"] = values["name"].strip()
        error = _check_refs(session, user.id, values)
        if error is not None:
            return error
        now = dt.datetime.now(dt.timezone.utc)
        new_id = session.execute(
            insert(sensor_installation)
            .values(user_id=user.id, created_at=now, updated_at=now, **values)
            .returning(sensor_installation.c.id)
        ).scalar_one()
        return DjangoStyleJSONResponse(
            _serialize(_owned_row(session, new_id, user.id)), status_code=201
        )


@router.patch("/sensor-installations/{pk}", summary="Move, rename or update a sensor")
def update_installation(
    pk: int,
    payload: InstallationPatch,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    with session_scope(commit=True) as session:
        guard = _block_if_technician(session, user.id)
        if guard is not None:
            return guard
        if not farm_map_available(session):
            return _json(FARM_MAP_UNAVAILABLE, 503)
        if _owned_row(session, pk, user.id) is None:
            return _json("Sensor installation not found.", 404)
        values = {k: getattr(payload, k) for k in payload.model_fields_set}
        for required in ("name", "sensor_type", "latitude", "longitude"):
            if required in values and values[required] is None:
                return _json(f"{required} cannot be null.", 400)
        if isinstance(values.get("name"), str):
            values["name"] = values["name"].strip()
        error = _check_refs(session, user.id, values)
        if error is not None:
            return error
        if values:
            session.execute(
                update(sensor_installation)
                .where(sensor_installation.c.id == pk)
                .values(updated_at=dt.datetime.now(dt.timezone.utc), **values)
            )
        return _serialize(_owned_row(session, pk, user.id))


@router.delete("/sensor-installations/{pk}", summary="Remove a sensor from the map")
def delete_installation(
    pk: int,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    with session_scope(commit=True) as session:
        guard = _block_if_technician(session, user.id)
        if guard is not None:
            return guard
        if not farm_map_available(session):
            return _json(FARM_MAP_UNAVAILABLE, 503)
        result = session.execute(
            delete(sensor_installation).where(
                sensor_installation.c.id == pk,
                sensor_installation.c.user_id == user.id,
            )
        )
        if result.rowcount == 0:
            return _json("Sensor installation not found.", 404)
        return {"success": True}


# ---------------------------------------------------------------------------
# Map preferences
# ---------------------------------------------------------------------------
_DEFAULT_PREFS: dict[str, Any] = {
    "basemap": "satellite_labels",
    "layers": {},
    "camera": None,
}


@router.get("/map-preferences", summary="Caller's farm-map display preferences")
def get_map_preferences(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        if not farm_map_available(session):
            return dict(_DEFAULT_PREFS)
        return get_map_preferences_row(session, user.id)


@router.put("/map-preferences", summary="Save farm-map display preferences")
def put_map_preferences(
    payload: MapPreferencesIn,
    user: AuthedUser = Depends(get_current_user),
):
    """Read-only logins may save their own view preferences: they change how
    the map looks for that user only, never farm data."""
    with session_scope(commit=True) as session:
        if not farm_map_available(session):
            return _json(FARM_MAP_UNAVAILABLE, 503)
        current = get_map_preferences_row(session, user.id)
        sent = payload.model_fields_set
        merged = {
            "basemap": payload.basemap
            if "basemap" in sent and payload.basemap
            else current["basemap"],
            "layers": (
                {k: v.model_dump() for k, v in (payload.layers or {}).items()}
                if "layers" in sent
                else current["layers"]
            ),
            "camera": (
                (payload.camera.model_dump() if payload.camera else None)
                if "camera" in sent
                else current["camera"]
            ),
        }
        if isinstance(merged["camera"], dict) and isinstance(
            merged["camera"].get("center"), tuple
        ):
            merged["camera"]["center"] = list(merged["camera"]["center"])
        now = dt.datetime.now(dt.timezone.utc)
        exists = session.execute(
            select(map_preference.c.user_id).where(map_preference.c.user_id == user.id)
        ).first()
        if exists is None:
            session.execute(
                insert(map_preference).values(user_id=user.id, updated_at=now, **merged)
            )
        else:
            session.execute(
                update(map_preference)
                .where(map_preference.c.user_id == user.id)
                .values(updated_at=now, **merged)
            )
        return merged


def get_map_preferences_row(session: Session, user_id: int) -> dict[str, Any]:
    row = session.execute(
        select(map_preference).where(map_preference.c.user_id == user_id)
    ).first()
    if row is None:
        return dict(_DEFAULT_PREFS)
    m = row._mapping
    return {"basemap": m["basemap"], "layers": m["layers"] or {}, "camera": m["camera"]}


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------
@router.get(
    "/farm-map/status",
    summary="Latest value, health and alert status of every placed sensor",
)
def farm_map_status(user: AuthedUser = Depends(get_current_user)):
    now = dt.datetime.now(dt.timezone.utc)
    with session_scope(commit=True) as session:
        sectors = session.execute(
            select(AnalyticsSector.id).where(AnalyticsSector.user_id == user.id)
        ).all()
        if not farm_map_available(session):
            return {
                "generated_at": now.isoformat(),
                "installations": [],
                "sectors": [
                    {
                        "id": s.id,
                        "status": "no_data",
                        "sensor_count": 0,
                        "alert_count": 0,
                    }
                    for s in sectors
                ],
            }
        rows = session.execute(
            select(sensor_installation).where(sensor_installation.c.user_id == user.id)
        ).all()

        # Active alerts, indexed by stream. ``recent_triggers_for_user`` stamps
        # last_triggered_at on first trigger (parity with /alerts), hence the
        # committing session.
        triggered: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
        try:
            for a in recent_triggers_for_user(session, user.id, now=now):
                if a.get("is_triggered"):
                    triggered.setdefault((a["sensor_key"], a["zone_id"]), []).append(
                        {"id": a["id"], "name": a["name"]}
                    )
        except Exception:  # pragma: no cover - alerts must never break the map
            log.exception("farm-map status: alert evaluation failed")

        cache: dict[tuple[str, int | None], tuple[Any, Any]] = {}
        installations: list[dict[str, Any]] = []
        by_sector: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            m = r._mapping
            key, zone_id = m["sensor_key"], m["zone_id"]
            latest = battery = signal = None
            alerts: list[dict[str, Any]] = []
            if key:
                value, ts = _reading(session, key, zone_id, user.id, cache)
                latest = _reading_json(value, ts, key)
                alerts = triggered.get((key, zone_id), [])
                status = classify_status(ts, now=now, triggered=bool(alerts))
                for health_key in ("battery", "signal"):
                    hv, hts = _reading(session, health_key, zone_id, user.id, cache)
                    if health_key == "battery":
                        battery = _reading_json(hv, hts, health_key)
                    else:
                        signal = _reading_json(hv, hts, health_key)
            else:
                status = classify_status(None, now=now, linked=False)
            item = {
                "id": m["id"],
                "status": status,
                "latest": latest,
                "battery": battery,
                "signal": signal,
                "alerts": alerts,
            }
            installations.append(item)
            if m["sector_id"] is not None:
                by_sector.setdefault(m["sector_id"], []).append(item)

        return {
            "generated_at": now.isoformat(),
            "installations": installations,
            "sectors": [
                {
                    "id": s.id,
                    "status": worst_status(
                        [i["status"] for i in by_sector.get(s.id, [])]
                    ),
                    "sensor_count": len(by_sector.get(s.id, [])),
                    "alert_count": sum(
                        len(i["alerts"]) for i in by_sector.get(s.id, [])
                    ),
                }
                for s in sectors
            ],
        }
