"""Assistant tools — the only way the assistant reads data.

Each tool is a named, self-describing unit (name + description + param schema +
handler). Handlers go through `registry.py` (the DB abstraction); they never
sprawl raw ORM across the codebase. Tools are exposed over HTTP by `router.py`
and selected by the orchestrator, so the same catalog drives both the
rule-based router today and an LLM tool-caller later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from apps.alerts.models import Alert
from .registry import SENSOR_SOURCES, latest_reading

# Tool handler: (django user, validated params) -> JSON-serializable data.
ToolHandler = Callable[[Any, dict], dict]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: ToolHandler
    # JSON-schema-ish param description, surfaced in the catalog (and to an LLM).
    params: dict[str, Any] = field(default_factory=dict)

    def to_catalog(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "params": self.params,
        }


class ToolRegistry:
    """In-memory catalog of tools, keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def catalog(self) -> list[dict[str, Any]]:
        return [t.to_catalog() for t in self._tools.values()]


registry = ToolRegistry()


# ── helpers ──────────────────────────────────────────────────────────────────
def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _metric_status(key: str, value: float | None) -> str:
    """Coarse health classification for a metric, for at-a-glance UI colour."""
    if value is None:
        return "unknown"
    if key == "soilMoisture":
        if value < 20:
            return "critical"
        if value < 30:
            return "warning"
    if key == "vpd" and value > 1.5:
        return "warning"
    return "ok"


# ── tools ────────────────────────────────────────────────────────────────────
# Static app map — the one tool that needs no DB.
_APP_PAGES = [
    {"path": "/", "key": "dashboard", "icon": "🏠"},
    {"path": "/soil", "key": "soil", "icon": "🌱"},
    {"path": "/station", "key": "station", "icon": "🌤️"},
    {"path": "/plant", "key": "plant", "icon": "🌿"},
    {"path": "/water", "key": "water", "icon": "💧"},
    {"path": "/vannes-pompes", "key": "vannesPompes", "icon": "🚰"},
    {"path": "/alerts", "key": "alerts", "icon": "🔔"},
    {"path": "/notifications", "key": "notifications", "icon": "📩"},
    {"path": "/settings", "key": "settings", "icon": "⚙️"},
]

_FARM_KEYS = ["soilMoisture", "soilTemp", "vpd", "et0", "airTemp", "humidity"]
_WEATHER_KEYS = ["airTemp", "humidity", "pressure", "et0", "vpd"]


def _get_sitemap(user, params: dict) -> dict:
    return {"routes": _APP_PAGES}


def _get_active_alerts(user, params: dict) -> dict:
    alerts = []
    for a in Alert.objects.filter(user=user, is_active=True).order_by("-id"):
        alerts.append(
            {
                "id": a.id,
                "name": a.name,
                "sensor_key": a.sensor_key,
                "zone": a.zone.name if a.zone_id else None,
                "condition": a.condition,
                "threshold": float(a.condition_nbr)
                if a.condition_nbr is not None
                else None,
                "last_triggered_at": a.last_triggered_at.isoformat()
                if a.last_triggered_at
                else None,
                "severity": "warning" if a.last_triggered_at else "ok",
            }
        )
    return {"alerts": alerts}


def _get_farm_status(user, params: dict) -> dict:
    zone_id = params.get("zone_id")
    metrics = []
    for key in _FARM_KEYS:
        reading = latest_reading(user, key, zone_id=zone_id)
        source = SENSOR_SOURCES[key]
        value = _round(reading.value) if reading else None
        metrics.append(
            {
                "key": key,
                "label": source.label,
                "value": value,
                "unit": source.unit,
                "status": _metric_status(key, value),
            }
        )
    return {"metrics": metrics, "zone_id": zone_id}


def _get_weather(user, params: dict) -> dict:
    metrics = []
    for key in _WEATHER_KEYS:
        reading = latest_reading(user, key)
        source = SENSOR_SOURCES[key]
        metrics.append(
            {
                "key": key,
                "label": source.label,
                "value": _round(reading.value) if reading else None,
                "unit": source.unit,
            }
        )
    return {"metrics": metrics}


def _zone_summary(z) -> dict:
    return {
        "id": z.id,
        "name": z.name,
        "area_m2": _round(z.space),
        "critical_moisture": _round(z.critical_moisture_threshold),
        "soil_param_TAW": _round(z.soil_param_TAW),
        "soil_param_FC": _round(z.soil_param_FC),
        "soil_param_WP": _round(z.soil_param_WP),
        "soil_param_RAW": _round(z.soil_param_RAW),
    }


def _list_zones(user, params: dict) -> dict:
    """The caller's zones, optionally filtered by a case-insensitive name substring."""
    from apps.irrigation.models import Zone

    name_filter = (params.get("zone_name") or "").strip().lower()
    zones = [
        _zone_summary(z)
        for z in Zone.objects.filter(user=user).order_by("id")
        if not name_filter or name_filter in (z.name or "").lower()
    ]
    return {"zones": zones}


def _get_zone_detail(user, params: dict) -> dict:
    """Resolve one zone by id or (case-insensitive) name and return full details."""
    from apps.irrigation.models import Zone

    zone_id = params.get("zone_id")
    zone_name = (params.get("zone_name") or "").strip()
    qs = Zone.objects.filter(user=user)
    z = None
    if zone_id:
        z = qs.filter(id=zone_id).first()
    elif zone_name:
        z = next((c for c in qs if (c.name or "").lower() == zone_name.lower()), None)
    if z is None:
        return {"zone": None}
    detail = _zone_summary(z)
    detail.update(
        {
            "pomp_flow_rate": _round(z.pomp_flow_rate),
            "irrigation_water_quantity": _round(z.irrigation_water_quantity),
        }
    )
    return {"zone": detail}


# Domain key lists for the snapshot tools. Keys must exist in SENSOR_SOURCES;
# any unknown key is skipped so a partial registry never breaks a snapshot.
_SOIL_KEYS = [
    "soilMoistureMedium",
    "soilMoistureHigh",
    "soilMoistureLow",
    "soilTempMedium",
    "soilTempHigh",
    "soilTempLow",
    "phSoil",
    "soilSalinity",
    "soilConductivity",
    "ecSoilMedium",
    "ecSoilHigh",
    "ecSoilLow",
]
_PLANT_KEYS = ["leafMoisture", "leafTemperature", "fruitSize", "largeFruitDiameter"]
_WATER_KEYS = [
    "waterFlow",
    "waterPressure",
    "waterEC",
    "waterPH",
    "precipitation",
    "waterLevel",
]


def _metric_snapshot(user, keys: list[str], zone_id: int | None) -> dict:
    """Latest reading per key as the shared {metrics:[...]} card shape."""
    metrics = []
    for key in keys:
        reading = latest_reading(user, key, zone_id=zone_id)
        if reading is None:  # key not in the registry
            continue
        source = SENSOR_SOURCES[key]
        value = _round(reading.value)
        metrics.append(
            {
                "key": key,
                "label": source.label,
                "value": value,
                "unit": source.unit,
                "status": _metric_status(key, value),
            }
        )
    return {"metrics": metrics, "zone_id": zone_id}


def _get_soil_status(user, params: dict) -> dict:
    """Latest soil sensor readings (moisture/temperature at 3 depths, pH,
    salinity, conductivity, EC)."""
    return _metric_snapshot(user, _SOIL_KEYS, params.get("zone_id"))


def _get_plant_status(user, params: dict) -> dict:
    """Latest plant/canopy readings (leaf moisture/temperature, fruit metrics)."""
    return _metric_snapshot(user, _PLANT_KEYS, params.get("zone_id"))


def _get_water_status(user, params: dict) -> dict:
    """Latest water readings (flow, pressure, EC, pH, precipitation, level)."""
    return _metric_snapshot(user, _WATER_KEYS, params.get("zone_id"))


registry.register(
    Tool(
        name="get_sitemap",
        description="List the navigable pages of the Agrilogy app.",
        handler=_get_sitemap,
    )
)
registry.register(
    Tool(
        name="get_active_alerts",
        description="List the caller's active alerts and whether they have triggered.",
        handler=_get_active_alerts,
    )
)
registry.register(
    Tool(
        name="get_farm_status",
        description="Snapshot of the latest key sensor readings (soil moisture, "
        "temperature, VPD, ET0, air temperature, humidity) for the caller.",
        handler=_get_farm_status,
        params={
            "zone_id": {
                "type": "integer",
                "required": False,
                "description": "Restrict the snapshot to a single zone.",
            }
        },
    )
)
registry.register(
    Tool(
        name="get_weather",
        description="Latest weather-station readings (air temperature, humidity, "
        "pressure, ET0, VPD) for the caller.",
        handler=_get_weather,
    )
)
registry.register(
    Tool(
        name="list_zones",
        description="List the caller's zones (name, area, critical-moisture "
        "threshold, soil params), optionally filtered by name.",
        handler=_list_zones,
        params={
            "zone_name": {
                "type": "string",
                "required": False,
                "description": "Case-insensitive name substring to filter zones.",
            }
        },
    )
)
registry.register(
    Tool(
        name="get_zone_detail",
        description="Full details of one zone (soil params + irrigation settings), "
        "resolved by id or name.",
        handler=_get_zone_detail,
        params={
            "zone_id": {
                "type": "integer",
                "required": False,
                "description": "Zone id (or pass zone_name).",
            },
            "zone_name": {
                "type": "string",
                "required": False,
                "description": "Zone name, case-insensitive (or pass zone_id).",
            },
        },
    )
)

_ZONE_PARAM = {
    "zone_id": {
        "type": "integer",
        "required": False,
        "description": "Restrict the snapshot to a single zone.",
    }
}
registry.register(
    Tool(
        name="get_soil_status",
        description="Snapshot of the latest soil sensor readings (moisture and "
        "temperature at three depths, pH, salinity, conductivity, EC).",
        handler=_get_soil_status,
        params=_ZONE_PARAM,
    )
)
registry.register(
    Tool(
        name="get_plant_status",
        description="Snapshot of the latest plant/canopy readings (leaf moisture, "
        "leaf temperature, fruit size, large fruit diameter).",
        handler=_get_plant_status,
        params=_ZONE_PARAM,
    )
)
registry.register(
    Tool(
        name="get_water_status",
        description="Snapshot of the latest water readings (flow, pressure, "
        "conductivity, pH, precipitation rate, water level).",
        handler=_get_water_status,
        params=_ZONE_PARAM,
    )
)
