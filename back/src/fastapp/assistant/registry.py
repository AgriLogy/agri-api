"""Data registry — the fastapp assistant's abstraction over the database.

Strangler port of ``apps/assistant/registry.py``. Maps the same stable,
UI-facing *sensor keys* to their backing agri-db SQLAlchemy model + presentation
metadata (label, unit). Keys, labels, and units are byte-identical to the Django
registry (English labels, same insertion order — the order is surfaced verbatim
in the tool catalog via ``", ".join(SENSOR_SOURCES.keys())``).

``latest_reading()`` is the one read primitive every tool builds on. Unlike the
Django original it takes an active SQLAlchemy ``Session`` (no Django ORM) and
returns a detached value snapshot (``Reading``) that is safe to use after the
session closes.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

import agri.db.analytics as analytics


@dataclass(frozen=True)
class SensorSource:
    """A readable sensor: stable key -> backing model + presentation metadata."""

    key: str
    model: type
    label: str
    unit: str


# Stable key -> source. Keys are shared with the frontend cards so the two
# sides speak the same vocabulary. Order + labels + units match the Django
# ``apps/assistant/registry.py`` exactly (byte-parity of the tool catalog).
SENSOR_SOURCES: dict[str, SensorSource] = {
    "soilMoisture": SensorSource(
        "soilMoisture", analytics.AnalyticsSoilmoisturemedium, "Soil moisture", "%"
    ),
    "soilTemp": SensorSource(
        "soilTemp", analytics.AnalyticsSoiltemperaturemedium, "Soil temperature", "°C"
    ),
    "vpd": SensorSource("vpd", analytics.AnalyticsVpdweather, "VPD", "kPa"),
    "et0": SensorSource("et0", analytics.AnalyticsEt0calculated, "ET0", "mm"),
    "airTemp": SensorSource(
        "airTemp", analytics.AnalyticsTemperatureweather, "Air temperature", "°C"
    ),
    "humidity": SensorSource(
        "humidity", analytics.AnalyticsHumidityweather, "Air humidity", "%"
    ),
    "pressure": SensorSource(
        "pressure", analytics.AnalyticsPressureweather, "Pressure", "hPa"
    ),
    # ── soil (moisture/temperature at 3 depths, chemistry) ──────────────────
    "soilMoistureMedium": SensorSource(
        "soilMoistureMedium",
        analytics.AnalyticsSoilmoisturemedium,
        "Soil moisture (medium)",
        "%",
    ),
    "soilMoistureHigh": SensorSource(
        "soilMoistureHigh",
        analytics.AnalyticsSoilmoisturehigh,
        "Soil moisture (shallow)",
        "%",
    ),
    "soilMoistureLow": SensorSource(
        "soilMoistureLow",
        analytics.AnalyticsSoilmoisturelow,
        "Soil moisture (deep)",
        "%",
    ),
    "soilTempMedium": SensorSource(
        "soilTempMedium",
        analytics.AnalyticsSoiltemperaturemedium,
        "Soil temperature (medium)",
        "°C",
    ),
    "soilTempHigh": SensorSource(
        "soilTempHigh",
        analytics.AnalyticsSoiltemperaturehigh,
        "Soil temperature (shallow)",
        "°C",
    ),
    "soilTempLow": SensorSource(
        "soilTempLow",
        analytics.AnalyticsSoiltemperaturelow,
        "Soil temperature (deep)",
        "°C",
    ),
    "phSoil": SensorSource("phSoil", analytics.AnalyticsPhsoil, "Soil pH", "pH"),
    "soilSalinity": SensorSource(
        "soilSalinity", analytics.AnalyticsSoilsalinitysensor, "Soil salinity", "dS/m"
    ),
    "soilConductivity": SensorSource(
        "soilConductivity",
        analytics.AnalyticsSoilconductivitysensor,
        "Soil conductivity",
        "µS/cm",
    ),
    "ecSoilMedium": SensorSource(
        "ecSoilMedium", analytics.AnalyticsEcsoilmedium, "Soil EC (medium)", "dS/m"
    ),
    "ecSoilHigh": SensorSource(
        "ecSoilHigh", analytics.AnalyticsEcsoilhigh, "Soil EC (shallow)", "dS/m"
    ),
    "ecSoilLow": SensorSource(
        "ecSoilLow", analytics.AnalyticsEcsoillow, "Soil EC (deep)", "dS/m"
    ),
    # ── plant / canopy ──────────────────────────────────────────────────────
    "leafMoisture": SensorSource(
        "leafMoisture", analytics.AnalyticsLeafmoisturesensor, "Leaf moisture", "%"
    ),
    "leafTemperature": SensorSource(
        "leafTemperature",
        analytics.AnalyticsLeaftemperaturesensor,
        "Leaf temperature",
        "°C",
    ),
    "fruitSize": SensorSource(
        "fruitSize", analytics.AnalyticsFruitsizesensor, "Fruit size", "mm"
    ),
    "largeFruitDiameter": SensorSource(
        "largeFruitDiameter",
        analytics.AnalyticsLargefruitdiametersensor,
        "Large fruit diameter",
        "mm",
    ),
    # ── water ───────────────────────────────────────────────────────────────
    "waterFlow": SensorSource(
        "waterFlow", analytics.AnalyticsWaterflowsensor, "Water flow", "L/s"
    ),
    "waterPressure": SensorSource(
        "waterPressure", analytics.AnalyticsWaterpressuresensor, "Water pressure", "Bar"
    ),
    "waterEC": SensorSource(
        "waterEC", analytics.AnalyticsWaterecsensor, "Water conductivity", "µS/cm"
    ),
    "waterPH": SensorSource(
        "waterPH", analytics.AnalyticsPhwatersensor, "Water pH", "pH"
    ),
    "precipitation": SensorSource(
        "precipitation",
        analytics.AnalyticsPrecipitationrate,
        "Precipitation rate",
        "mm/h",
    ),
    "waterLevel": SensorSource(
        "waterLevel", analytics.AnalyticsWaterlevelsensor, "Water level", "cm"
    ),
}


@dataclass(frozen=True)
class Reading:
    value: float | None
    unit: str
    timestamp: datetime.datetime | None


def latest_reading(
    session: Session, user_id: int, key: str, *, zone_id: int | None = None
) -> Reading | None:
    """Most recent reading of `key` for `user_id` (optionally scoped to a zone).

    Returns None for an unknown key; a Reading with value=None when the sensor
    exists but has no data yet. Mirrors ``apps/assistant/registry.latest_reading``.
    """
    source = SENSOR_SOURCES.get(key)
    if source is None:
        return None
    model = source.model
    criteria = [model.user_id == user_id]
    if zone_id is not None:
        criteria.append(model.zone_id == zone_id)
    row = session.scalars(
        select(model).where(*criteria).order_by(model.timestamp.desc()).limit(1)
    ).first()
    if row is None:
        return Reading(value=None, unit=source.unit, timestamp=None)
    return Reading(value=row.value, unit=source.unit, timestamp=row.timestamp)
