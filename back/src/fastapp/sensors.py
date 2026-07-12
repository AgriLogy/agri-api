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


def _iso(ts: datetime.datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else None


def hourly_readings(
    spec: SensorSpec,
    *,
    user_id: int,
    zone_id: int | None,
    start: datetime.date | None,
    end: datetime.date | None,
) -> list[dict[str, Any]]:
    """One averaged reading per clock hour — byte-parity port of
    ``apps.sensors.engine.hourly_readings``. Same [] on no rows, same key
    order per row: char fields, id, zone, user, timestamp, value cols,
    default_unit, available_units."""
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
        sample = session.scalars(
            _with_device_join(select(model), model)
            .where(*criteria)
            .order_by(model.id)
            .limit(1)
        ).first()
        if sample is None:
            return []
        meta = {c: getattr(sample, c, None) for c in spec.char_fields}
        sample_zone = sample.zone_id
        sample_user = sample.user_id

        buckets = AgriMainDBClient.hourly_averages(
            session,
            model,
            user_id=user_id,
            zone_id=zone_id,
            start=start_dt,
            end=end_dt,
            value_columns=spec.value_fields or ("value",),
        )

    rows: list[dict[str, Any]] = []
    for b in buckets:
        row: dict[str, Any] = dict(meta)
        row["id"] = b["last_id"]
        row["zone"] = sample_zone
        row["user"] = sample_user
        row["timestamp"] = _iso(b["hour"])
        for c in spec.value_fields:
            row[c] = b.get(c)
        row["default_unit"] = spec.default_unit
        row["available_units"] = spec.available_units
        rows.append(row)
    return rows


def raw_readings(
    spec: SensorSpec,
    *,
    user_id: int,
    zone_id: int | None,
    start: datetime.date | None,
    end: datetime.date | None,
) -> list[dict[str, Any]]:
    """Un-aggregated rows at the sensor's native cadence — byte-parity port of
    the Django ``raw=true`` branch: filter by owner + inclusive date range +
    optional zone, ordered by timestamp ascending."""
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
        rows = session.scalars(
            _with_device_join(select(model), model)
            .where(*criteria)
            .order_by(model.timestamp)
        ).all()
        return [serialize_raw(r, spec) for r in rows]


# Maps a raw_order key (a Django field name) to the agri.db column attribute.
_RAW_ATTR = {"zone": "zone_id", "user": "user_id"}


def serialize_raw(row, spec: SensorSpec) -> dict[str, Any]:
    """Serialize one agri.db row to the ``raw=true`` wire shape — byte-parity
    port of ``_serialize_reading`` (model_to_dict order + id + ISO timestamp +
    units)."""
    out: dict[str, Any] = {}
    for key in spec.raw_order:
        if key == "timestamp":
            out[key] = _iso(getattr(row, "timestamp", None))
        else:
            out[key] = getattr(row, _RAW_ATTR.get(key, key), None)
    out["id"] = row.id
    out["default_unit"] = spec.default_unit
    out["available_units"] = spec.available_units
    return out
