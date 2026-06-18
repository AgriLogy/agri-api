"""Data registry — the assistant's abstraction over the database.

The assistant never touches ORM models directly. Instead it reads through a
small registry that maps stable, UI-facing *sensor keys* to their backing
Django model + presentation metadata (label, unit). This is the single seam
between "what the assistant asks for" and "where the data lives", so the model
layer can move underneath without changing the tools or the frontend.

`latest_reading()` is the one read primitive every tool builds on.
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.sensors.models import (
    Et0Calculated,
    HumidityWeather,
    PressureWeather,
    SoilMoistureMedium,
    SoilTemperatureMedium,
    TemperatureWeather,
    VPDWeather,
)


@dataclass(frozen=True)
class SensorSource:
    """A readable sensor: stable key -> backing model + presentation metadata."""

    key: str
    model: type
    label: str
    unit: str


# Stable key -> source. Keys are shared with the frontend cards so the two
# sides speak the same vocabulary.
SENSOR_SOURCES: dict[str, SensorSource] = {
    "soilMoisture": SensorSource(
        "soilMoisture", SoilMoistureMedium, "Soil moisture", "%"
    ),
    "soilTemp": SensorSource(
        "soilTemp", SoilTemperatureMedium, "Soil temperature", "°C"
    ),
    "vpd": SensorSource("vpd", VPDWeather, "VPD", "kPa"),
    "et0": SensorSource("et0", Et0Calculated, "ET0", "mm"),
    "airTemp": SensorSource("airTemp", TemperatureWeather, "Air temperature", "°C"),
    "humidity": SensorSource("humidity", HumidityWeather, "Air humidity", "%"),
    "pressure": SensorSource("pressure", PressureWeather, "Pressure", "hPa"),
}


@dataclass(frozen=True)
class Reading:
    value: float | None
    unit: str
    timestamp: object | None  # datetime | None


def latest_reading(user, key: str, *, zone_id: int | None = None) -> Reading | None:
    """Most recent reading of `key` for `user` (optionally scoped to a zone).

    Returns None for an unknown key; a Reading with value=None when the sensor
    exists but has no data yet.
    """
    source = SENSOR_SOURCES.get(key)
    if source is None:
        return None
    qs = source.model.objects.filter(user=user)
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)
    row = qs.order_by("-timestamp").first()
    if row is None:
        return Reading(value=None, unit=source.unit, timestamp=None)
    return Reading(value=row.value, unit=source.unit, timestamp=row.timestamp)
