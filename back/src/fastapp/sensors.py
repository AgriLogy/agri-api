"""Sensor-domain data + serialization for fastapp (strangler port of the
Django ``apps.sensors`` engine + registry).

Static per-sensor spec (``SENSOR_SPEC``) extracted from the Django models'
``default_unit`` / ``available_units`` properties + field order, so fastapp
reproduces the ninja ``/sensors/<slug>`` wire shape byte-for-byte without
importing Django. The hourly aggregation itself is delegated to agri-core
(``AgriMainDBClient.hourly_averages`` over the agri.db SQLAlchemy models),
exactly as the Django engine does. Lift candidate → agri-core once a second
consumer needs it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

import agri.db.analytics as analytics
from agri.core.database import AgriMainDBClient, session_scope
from agri.core.database.client import _owner_user, _owner_zone, _with_device_join
from agri.db.devices import AnalyticsDevice
from fastapp.calibration import corrected_value, load_calibrations


@dataclass(frozen=True)
class SensorSpec:
    agri_db: str
    default_unit: str | None
    available_units: list[str] | None
    value_fields: list[str]
    char_fields: list[str]
    raw_order: list[str] = field(default_factory=list)


# slug → SensorSpec. Generated from the Django sensor models (field order +
# unit properties preserved for byte-parity). Keep in sync if a sensor model's
# fields/units change.
SENSOR_SPEC: dict[str, SensorSpec] = {
    "precipitationrate": SensorSpec(
        agri_db="AnalyticsPrecipitationrate",
        default_unit="mm/h",
        available_units=["mm/h"],
        value_fields=["value"],
        char_fields=["color", "courbe_name"],
        raw_order=["id", "zone", "user", "value", "timestamp", "color", "courbe_name"],
    ),
    "humidityweather": SensorSpec(
        agri_db="AnalyticsHumidityweather",
        default_unit="%",
        available_units=["%"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "windspeed": SensorSpec(
        agri_db="AnalyticsWindspeed",
        default_unit="m/s",
        available_units=["m/s", "km/h"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "solarradiation": SensorSpec(
        agri_db="AnalyticsSolarradiation",
        default_unit="W/m²",
        available_units=["W/m²"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "pressureweather": SensorSpec(
        agri_db="AnalyticsPressureweather",
        default_unit="hPa",
        available_units=["hPa", "bar", "kpa"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "winddirection": SensorSpec(
        agri_db="AnalyticsWinddirection",
        default_unit="°",
        available_units=["°"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "temperatureweather": SensorSpec(
        agri_db="AnalyticsTemperatureweather",
        default_unit="°C",
        available_units=["°C", "°F"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "ecsoilmedium": SensorSpec(
        agri_db="AnalyticsEcsoilmedium",
        default_unit="dS/m",
        available_units=["dS/m"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "soiltemperaturemedium": SensorSpec(
        agri_db="AnalyticsSoiltemperaturemedium",
        default_unit="°C",
        available_units=["°C", "°F"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "ecsoilhigh": SensorSpec(
        agri_db="AnalyticsEcsoilhigh",
        default_unit="dS/m",
        available_units=["dS/m"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "ecsoillow": SensorSpec(
        agri_db="AnalyticsEcsoillow",
        default_unit="dS/m",
        available_units=["dS/m"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "soilmoisturemedium": SensorSpec(
        agri_db="AnalyticsSoilmoisturemedium",
        default_unit="%",
        available_units=["%"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "soilmoisturehigh": SensorSpec(
        agri_db="AnalyticsSoilmoisturehigh",
        default_unit="%",
        available_units=["%"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "soilmoisturelow": SensorSpec(
        agri_db="AnalyticsSoilmoisturelow",
        default_unit="%",
        available_units=["%"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "phsoil": SensorSpec(
        agri_db="AnalyticsPhsoil",
        default_unit="pH",
        available_units=["pH"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "soiltemperaturelow": SensorSpec(
        agri_db="AnalyticsSoiltemperaturelow",
        default_unit="°C",
        available_units=["°C", "°F"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "soiltemperaturehigh": SensorSpec(
        agri_db="AnalyticsSoiltemperaturehigh",
        default_unit="°C",
        available_units=["°C", "°F"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "waterflow": SensorSpec(
        agri_db="AnalyticsWaterflowsensor",
        default_unit="m³/h",
        available_units=["L/s", "m³/h"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "waterec": SensorSpec(
        agri_db="AnalyticsWaterecsensor",
        default_unit="μS/cm",
        available_units=["μS/cm", "mS/cm"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "phwater": SensorSpec(
        agri_db="AnalyticsPhwatersensor",
        default_unit="pH",
        available_units=["pH"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "electricityconsumption": SensorSpec(
        agri_db="AnalyticsElectricityconsumptionsensor",
        default_unit="kWh",
        available_units=["kWh", "Wh"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "leafmoisture": SensorSpec(
        agri_db="AnalyticsLeafmoisturesensor",
        default_unit="%",
        available_units=["%"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "multidepthsoilmoisture": SensorSpec(
        agri_db="AnalyticsMultidepthsoilmoisturesensor",
        default_unit="%",
        available_units=["%"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "largefruitdiameter": SensorSpec(
        agri_db="AnalyticsLargefruitdiametersensor",
        default_unit="mm",
        available_units=["mm", "cm"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "waterlevel": SensorSpec(
        agri_db="AnalyticsWaterlevelsensor",
        default_unit="cm",
        available_units=["cm", "m"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "soilconductivity": SensorSpec(
        agri_db="AnalyticsSoilconductivitysensor",
        default_unit="μS/cm",
        available_units=["μS/cm", "mS/cm"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "et0weather": SensorSpec(
        agri_db="AnalyticsEt0weather",
        default_unit="mm/day",
        available_units=["mm/day"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "et0calculated": SensorSpec(
        agri_db="AnalyticsEt0calculated",
        default_unit="mm/day",
        available_units=["mm/day"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "vpdweather": SensorSpec(
        agri_db="AnalyticsVpdweather",
        default_unit="kPa",
        available_units=None,
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "leaftemperature": SensorSpec(
        agri_db="AnalyticsLeaftemperaturesensor",
        default_unit="°C",
        available_units=["C", "°F"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "soilsalinity": SensorSpec(
        agri_db="AnalyticsSoilsalinitysensor",
        default_unit="mg/m",
        available_units=["dS/m", "mS/cm"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "npk": SensorSpec(
        agri_db="AnalyticsNpksensor",
        default_unit="mg/kg",
        available_units=["mg/kg", "ppm"],
        value_fields=["nitrogen_value", "phosphorus_value", "potassium_value"],
        char_fields=[
            "nitrogen_color",
            "nitrogen_courbe_name",
            "phosphorus_color",
            "phosphorus_courbe_name",
            "potassium_color",
            "potassium_courbe_name",
        ],
        raw_order=[
            "id",
            "zone",
            "user",
            "timestamp",
            "nitrogen_value",
            "nitrogen_color",
            "nitrogen_courbe_name",
            "phosphorus_value",
            "phosphorus_color",
            "phosphorus_courbe_name",
            "potassium_value",
            "potassium_color",
            "potassium_courbe_name",
        ],
    ),
    "fruitsize": SensorSpec(
        agri_db="AnalyticsFruitsizesensor",
        default_unit="mm",
        available_units=["mm", "cm"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "ecsalinity": SensorSpec(
        agri_db="AnalyticsEcsalinitysensor",
        default_unit="μS/cm",
        available_units=["μS/cm", "dS/m"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "waterpressure": SensorSpec(
        agri_db="AnalyticsWaterpressuresensor",
        default_unit="Bar/s",
        available_units=["Bar/s"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "battery": SensorSpec(
        agri_db="AnalyticsBatterysensor",
        default_unit="V",
        available_units=["V"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
    "signal": SensorSpec(
        agri_db="AnalyticsSignalsensor",
        default_unit="dBm",
        available_units=["dBm"],
        value_fields=["value"],
        char_fields=[],
        raw_order=["id", "zone", "user", "value", "timestamp"],
    ),
}


def agri_db_model(spec: SensorSpec):
    """The agri.db.analytics SQLAlchemy class for a sensor spec."""
    return getattr(analytics, spec.agri_db)


# --- slug → sensor_key (calibration key) -----------------------------------
# Calibration (analytics_sensorcalibration) is keyed by (device_id, sensor_key)
# where sensor_key is a SENSOR_KEY_REGISTRY key — the SAME key the ingest/alert
# path uses. The read path is keyed by SENSOR_SPEC slug, so it must resolve the
# matching sensor_key or the dashboard would look up a different calibration
# than the alert did. The mapping is the registry model name (minus the
# ``Analytics`` prefix) matched case-insensitively; two models are shared by
# two registry keys and one slug (npk) has no registry key at all, so those are
# pinned/None explicitly (guessing would apply the wrong factor).
_SLUG_KEY_OVERRIDES: dict[str, str | None] = {
    # AnalyticsPhsoil is both "soil_ph" and "ph_soil"; the LoRa ingest writes
    # "ph_soil", so the read path must calibrate under that same key.
    "phsoil": "ph_soil",
    # AnalyticsEt0calculated is both "et0" and "et0_calculated"; ingest/beat use
    # "et0_calculated".
    "et0calculated": "et0_calculated",
    # NPK has no SENSOR_KEY_REGISTRY entry → not calibratable via this scheme.
    "npk": None,
}


def _build_slug_key_map() -> dict[str, str | None]:
    from agri.core.alerts import SENSOR_KEY_REGISTRY

    by_model: dict[str, list[str]] = {}
    for key, meta in SENSOR_KEY_REGISTRY.items():
        by_model.setdefault(str(meta["model"]).lower(), []).append(key)

    out: dict[str, str | None] = {}
    for slug, spec in SENSOR_SPEC.items():
        if slug in _SLUG_KEY_OVERRIDES:
            out[slug] = _SLUG_KEY_OVERRIDES[slug]
            continue
        model_key = spec.agri_db.removeprefix("Analytics").lower()
        keys = by_model.get(model_key)
        # A single unambiguous registry key resolves; anything else is left
        # uncalibrated rather than guessed.
        out[slug] = keys[0] if keys and len(keys) == 1 else None
    return out


# slug → the sensor_key its readings are calibrated under (None = not
# calibratable). Built once at import from SENSOR_SPEC × SENSOR_KEY_REGISTRY.
SENSOR_KEY_FOR_SLUG: dict[str, str | None] = _build_slug_key_map()


def _iso(ts: datetime.datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else None


def effective_owner(session, row) -> tuple[int | None, int | None]:
    """``(user_id, zone_id)`` of the row's EFFECTIVE owner.

    Python-side twin of the SQL ``COALESCE(device.owner, row.owner)`` used by
    ``_owner_user`` / ``_owner_zone``: readings are device-keyed, so a row
    belongs to whoever owns its ``analytics_device``. The row's own
    ``user_id`` / ``zone_id`` are only a stale commissioning snapshot and must
    never be reported on their own — filtering resolves through the device, so
    the projection has to as well or the two halves disagree.
    """
    row_user = getattr(row, "user_id", None)
    row_zone = getattr(row, "zone_id", None)
    device_id = getattr(row, "device_id", None)
    if device_id is None:
        return row_user, row_zone
    device = session.get(AnalyticsDevice, device_id)
    if device is None:
        return row_user, row_zone
    return (
        device.user_id if device.user_id is not None else row_user,
        device.zone_id if device.zone_id is not None else row_zone,
    )


def hourly_readings(
    spec: SensorSpec,
    *,
    user_id: int,
    zone_id: int | None,
    start: datetime.date | None,
    end: datetime.date | None,
    sensor_key: str | None = None,
) -> list[dict[str, Any]]:
    """One averaged reading per clock hour — byte-parity port of
    ``apps.sensors.engine.hourly_readings``. Same [] on no rows, same key
    order per row: char fields, id, zone, user, timestamp, value cols,
    default_unit, available_units.

    When ``sensor_key`` resolves to a calibratable key, each hourly average is
    corrected by the calibration of the device that produced that hour's rows
    (identified via the bucket's ``last_id``). The affine correction commutes
    with averaging, so ``corrected(avg(raw)) == avg(corrected(raw))`` for a
    single device — the common case under device-keyed ownership. Correction
    goes through :mod:`fastapp.calibration`, the SAME helper the alert path
    uses, so a hourly point and its alert never disagree."""
    model = agri_db_model(spec)
    start_dt = (
        datetime.datetime.combine(
            start, datetime.time.min, tzinfo=datetime.timezone.utc
        )
        if start
        else None
    )
    end_dt = (
        datetime.datetime.combine(
            end + datetime.timedelta(days=1),
            datetime.time.min,
            tzinfo=datetime.timezone.utc,
        )
        if end
        else None
    )

    with session_scope() as session:
        # Presentation metadata is constant per user/zone → one sample row
        # supplies char fields + zone/user, mirroring the Django engine's
        # ``.first()`` (an unordered queryset .first() orders by pk). Ownership
        # is resolved via the device (COALESCE) so a transferred device's rows
        # are still found under the new owner.
        criteria = [_owner_user(model) == user_id]
        if zone_id is not None:
            criteria.append(_owner_zone(model) == zone_id)
        sample_row = session.execute(
            _with_device_join(
                select(
                    model,
                    _owner_user(model).label("eff_user"),
                    _owner_zone(model).label("eff_zone"),
                ),
                model,
            )
            .where(*criteria)
            .order_by(model.id)
            .limit(1)
        ).first()
        if sample_row is None:
            return []
        sample, eff_user, eff_zone = sample_row
        meta = {c: getattr(sample, c, None) for c in spec.char_fields}
        # Report the EFFECTIVE owner (device-resolved), never the row's raw
        # snapshot: the rows were selected by effective owner, so labelling
        # them with a stale user/zone would name a different client.
        sample_zone = zone_id if zone_id is not None else eff_zone
        sample_user = user_id if user_id is not None else eff_user

        buckets = AgriMainDBClient.hourly_averages(
            session,
            model,
            user_id=user_id,
            zone_id=zone_id,
            start=start_dt,
            end=end_dt,
            value_columns=spec.value_fields or ("value",),
        )

        # Resolve the calibration for each bucket while the session is open:
        # map the bucket's representative row (``last_id``) to its device, then
        # batch-load every needed (device_id, sensor_key) in ONE query. No
        # per-reading query — everything below maps in memory.
        calibration_by_last_id = _calibrations_for_buckets(
            session, model, buckets, sensor_key
        )

    rows: list[dict[str, Any]] = []
    for b in buckets:
        row: dict[str, Any] = dict(meta)
        row["id"] = b["last_id"]
        row["zone"] = sample_zone
        row["user"] = sample_user
        row["timestamp"] = _iso(b["hour"])
        calibration = calibration_by_last_id.get(b["last_id"])
        for c in spec.value_fields:
            row[c] = corrected_value(
                b.get(c),
                calibration,
                sensor_key=sensor_key,
                native_unit=spec.default_unit,
            )
        row["default_unit"] = spec.default_unit
        row["available_units"] = spec.available_units
        rows.append(row)
    return rows


def _calibrations_for_buckets(
    session,
    model,
    buckets: list[dict[str, Any]],
    sensor_key: str | None,
) -> dict[Any, Any]:
    """``{last_id: Calibration}`` for the hourly buckets — one id→device query
    plus one batch calibration load, or ``{}`` when nothing is calibratable."""
    if not sensor_key or not buckets:
        return {}
    last_ids = [b["last_id"] for b in buckets if b.get("last_id") is not None]
    if not last_ids:
        return {}
    device_by_last_id = {
        row_id: device_id
        for row_id, device_id in session.execute(
            select(model.id, model.device_id).where(model.id.in_(last_ids))
        ).all()
    }
    calibrations = load_calibrations(
        session,
        {(device_id, sensor_key) for device_id in device_by_last_id.values()},
    )
    return {
        last_id: calibrations.get((device_id, sensor_key))
        for last_id, device_id in device_by_last_id.items()
        if device_id is not None
    }


def raw_readings(
    spec: SensorSpec,
    *,
    user_id: int,
    zone_id: int | None,
    start: datetime.date | None,
    end: datetime.date | None,
    sensor_key: str | None = None,
) -> list[dict[str, Any]]:
    """Un-aggregated rows at the sensor's native cadence — byte-parity port of
    the Django ``raw=true`` branch: filter by owner + inclusive date range +
    optional zone, ordered by timestamp ascending.

    Each row is corrected by ITS OWN device's calibration (per-row
    ``device_id``), batch-loaded in one query, through the same
    :mod:`fastapp.calibration` helper the hourly + alert paths use."""
    model = agri_db_model(spec)
    # Ownership resolved via the device (COALESCE) so a transferred device's
    # history follows it; non-device rows fall back to their own user/zone.
    criteria = [_owner_user(model) == user_id]
    if start is not None:
        criteria.append(
            model.timestamp
            >= datetime.datetime.combine(
                start, datetime.time.min, tzinfo=datetime.timezone.utc
            )
        )
    if end is not None:
        # inclusive end date → strictly before (end + 1 day) 00:00
        criteria.append(
            model.timestamp
            < datetime.datetime.combine(
                end + datetime.timedelta(days=1),
                datetime.time.min,
                tzinfo=datetime.timezone.utc,
            )
        )
    if zone_id is not None:
        criteria.append(_owner_zone(model) == zone_id)
    with session_scope() as session:
        rows = session.execute(
            _with_device_join(
                select(
                    model,
                    _owner_user(model).label("eff_user"),
                    _owner_zone(model).label("eff_zone"),
                ),
                model,
            )
            .where(*criteria)
            .order_by(model.timestamp)
        ).all()
        # Batch-load one calibration per (device_id, sensor_key) present, then
        # correct each row by its own device's factor — no per-row query.
        calibrations = load_calibrations(
            session,
            {(getattr(r, "device_id", None), sensor_key) for r, _, _ in rows},
        )
        serialized: list[dict[str, Any]] = []
        for r, eff_user, eff_zone in rows:
            out = serialize_raw(r, spec, eff_user=eff_user, eff_zone=eff_zone)
            device_id = getattr(r, "device_id", None)
            calibration = (
                calibrations.get((device_id, sensor_key))
                if sensor_key and device_id is not None
                else None
            )
            if calibration is not None:
                for c in spec.value_fields:
                    out[c] = corrected_value(
                        out[c],
                        calibration,
                        sensor_key=sensor_key,
                        native_unit=spec.default_unit,
                    )
            serialized.append(out)
        return serialized


# Maps a raw_order key (a Django field name) to the agri.db column attribute.
_RAW_ATTR = {"zone": "zone_id", "user": "user_id"}


def serialize_raw(
    row,
    spec: SensorSpec,
    *,
    eff_user: int | None = None,
    eff_zone: int | None = None,
) -> dict[str, Any]:
    """Serialize one agri.db row to the ``raw=true`` wire shape — byte-parity
    port of ``_serialize_reading`` (model_to_dict order + id + ISO timestamp +
    units).

    ``user`` / ``zone`` carry the DEVICE-RESOLVED owner. Callers that already
    hold the resolved values (the SELECT projected them) pass them in; the
    fallback to the row's own columns applies only to non-device rows.
    """
    resolved = {"user": eff_user, "zone": eff_zone}
    out: dict[str, Any] = {}
    for key in spec.raw_order:
        if key == "timestamp":
            out[key] = _iso(getattr(row, "timestamp", None))
        elif resolved.get(key) is not None:
            out[key] = resolved[key]
        else:
            out[key] = getattr(row, _RAW_ATTR.get(key, key), None)
    out["id"] = row.id
    out["default_unit"] = spec.default_unit
    out["available_units"] = spec.available_units
    return out
