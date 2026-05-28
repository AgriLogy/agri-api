"""Sensor-related v2 endpoints (django-ninja style).

First router lands ``GET /api/v2/sensors/keys`` returning the canonical
sensor-key registry from ``agri.core.alerts``. Proves the django-ninja
+ agri-core handler shape end to end.
"""
from __future__ import annotations

from ninja import Router, Schema

from agri.core.alerts import SENSOR_KEY_REGISTRY

router = Router()


class SensorKey(Schema):
    """Public shape of one registry entry."""

    key: str
    label: str
    unit: str
    type: str | None = None


class SensorKeyList(Schema):
    """Response envelope for the sensor-keys list."""

    items: list[SensorKey]


@router.get("/keys", response=SensorKeyList, summary="List known sensor keys")
def list_sensor_keys(request) -> SensorKeyList:
    """Return every sensor key the system recognises.

    The registry lives in ``agri.core.alerts.SENSOR_KEY_REGISTRY`` so
    this handler is a pure projection. agri-core owns the source of
    truth; this view only translates it into a typed pydantic shape
    for the wire format.
    """
    items = [
        SensorKey(
            key=key,
            label=spec["label"],
            unit=spec["unit"],
            type=spec.get("type"),
        )
        for key, spec in SENSOR_KEY_REGISTRY.items()
    ]
    return SensorKeyList(items=items)
