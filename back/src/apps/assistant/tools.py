"""Assistant tools — the only way the assistant reads data.

Each tool is a named, self-describing unit (name + description + param schema +
handler). Handlers go through `registry.py` (the DB abstraction); they never
sprawl raw ORM across the codebase. Tools are exposed over HTTP by `router.py`
and selected by the orchestrator, so the same catalog drives both the
rule-based router today and an LLM tool-caller later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from django.db.models import Avg, Count, Max, Min
from django.utils import timezone

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


def _get_sensor_trend(user, params: dict) -> dict:
    """Rolling-window trend for one sensor key: latest/min/max/avg/count plus a
    direction (rising|falling|flat) from the first vs last value in the window."""
    sensor_key = params.get("sensor_key")
    zone_id = params.get("zone_id")
    try:
        hours = int(params.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 24

    source = SENSOR_SOURCES.get(sensor_key)
    if source is None:
        return {"error": f"Unknown sensor key: {sensor_key}", "key": sensor_key}

    now = timezone.now()
    window_start = now - timedelta(hours=hours)
    qs = source.model.objects.filter(
        user=user, timestamp__gte=window_start, timestamp__lte=now
    )
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)

    stats = qs.aggregate(
        min_val=Min("value"),
        max_val=Max("value"),
        avg_val=Avg("value"),
        count=Count("id"),
    )
    latest = latest_reading(user, sensor_key, zone_id=zone_id)
    first_row = qs.order_by("timestamp").first()
    last_row = qs.order_by("-timestamp").first()

    direction = "flat"
    if (
        first_row is not None
        and last_row is not None
        and first_row.value is not None
        and last_row.value is not None
    ):
        if last_row.value > first_row.value:
            direction = "rising"
        elif last_row.value < first_row.value:
            direction = "falling"

    return {
        "key": sensor_key,
        "label": source.label,
        "unit": source.unit,
        "latest": _round(latest.value if latest else None),
        "min": _round(stats["min_val"]),
        "max": _round(stats["max_val"]),
        "avg": _round(stats["avg_val"]),
        "count": stats["count"] or 0,
        "direction": direction,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
    }


_DEFAULT_CRITICAL_MOISTURE_PCT = 20.0
# decision_reason values from agri-core that mean "irrigate now" / "hold".
_IRRIGATE_REASONS = {"stress", "soil_moisture_low", "complementary"}
_HOLD_REASONS = {"no_stress", "rain_will_suffice"}


def _irrigation_unknown(reason: str) -> dict:
    """A complete, null-filled payload for the no-data / no-zone case."""
    return {
        "recommendation": "unknown",
        "reason": reason,
        "soil_moisture_pct": None,
        "critical_moisture_threshold": None,
        "soil_moisture_status": "unknown",
        "et0_mm": None,
        "vpd_kpa": None,
        "dr_today_mm": None,
        "raw_mm": None,
        "zone_name": None,
        "zone_area_m2": None,
        "estimated_water_m3": None,
        "estimated_duration_min": None,
        "morning_volume_m3": None,
        "evening_volume_m3": None,
        "decision_source": "no_zone",
        "last_update_timestamp": None,
    }


def _get_irrigation_advice(user, params: dict) -> dict:
    """Per-zone irrigation recommendation.

    Two-tier: prefer agri-core's Dr/RAW field snapshot; if that is unavailable
    (no agri-core DB, missing data, raises), fall back to a simple
    soil-moisture-vs-critical-threshold rule. Always returns a valid payload —
    never raises — and never writes.
    """
    from apps.irrigation.models import Zone

    zone_id = params.get("zone_id")
    zone_qs = Zone.objects.filter(user=user)
    zone = (
        zone_qs.filter(id=zone_id).first()
        if zone_id
        else zone_qs.order_by("id").first()
    )
    if zone is None:
        return _irrigation_unknown("Aucune zone configurée pour ce compte.")

    soil_reading = latest_reading(user, "soilMoisture", zone_id=zone.id)
    et0_reading = latest_reading(user, "et0", zone_id=zone.id)
    vpd_reading = latest_reading(user, "vpd", zone_id=zone.id)
    soil_moisture_pct = _round(soil_reading.value) if soil_reading else None
    et0_mm = _round(et0_reading.value) if et0_reading else None
    vpd_kpa = _round(vpd_reading.value) if vpd_reading else None
    critical_threshold = (
        zone.critical_moisture_threshold
        if zone.critical_moisture_threshold is not None
        else _DEFAULT_CRITICAL_MOISTURE_PCT
    )

    recommendation = "unknown"
    reason = "Données insuffisantes pour une recommandation."
    decision_source = "fallback"
    dr_today_mm = raw_mm = None
    estimated_water_m3 = estimated_duration_min = None
    morning_volume_m3 = evening_volume_m3 = None

    # Tier 1: agri-core Dr/RAW snapshot (best-effort, never fatal).
    try:
        from agriapi.agronomy import field_snapshot

        snap = field_snapshot(user)
    except Exception:
        snap = None
    if snap:
        decision_reason = snap.get("decision_reason")
        if decision_reason in _IRRIGATE_REASONS:
            recommendation = "irrigate"
        elif decision_reason in _HOLD_REASONS:
            recommendation = "hold"
        if recommendation != "unknown":
            decision_source = "field_snapshot_dr"
            reason = snap.get("irrigation_decision") or reason
            dr_today_mm = _round(snap.get("dr_today_mm"))
            raw_mm = _round(snap.get("raw_mm"))
            estimated_water_m3 = _round(snap.get("recommended_volume_m3"))
            estimated_duration_min = _round(snap.get("recommended_duration_min"))
            morning_volume_m3 = _round(snap.get("morning_volume_m3"))
            evening_volume_m3 = _round(snap.get("evening_volume_m3"))
            if soil_moisture_pct is None and snap.get("soil_moisture_pct") is not None:
                soil_moisture_pct = _round(snap.get("soil_moisture_pct"))
            if et0_mm is None and snap.get("et0_today_mm") is not None:
                et0_mm = _round(snap.get("et0_today_mm"))

    # Tier 2: critical-threshold fallback when the snapshot gave no decision.
    if recommendation == "unknown" and soil_moisture_pct is not None:
        decision_source = "critical_threshold_fallback"
        if soil_moisture_pct < critical_threshold:
            recommendation = "irrigate"
            reason = (
                f"Humidité du sol {soil_moisture_pct:.1f} % < "
                f"seuil critique {critical_threshold:.1f} %."
            )
            if zone.irrigation_water_quantity:
                estimated_water_m3 = _round(zone.irrigation_water_quantity / 1000.0)
                if zone.pomp_flow_rate:
                    flow_m3h = zone.pomp_flow_rate * 3.6  # L/s → m³/h
                    estimated_duration_min = _round(
                        (estimated_water_m3 / flow_m3h) * 60.0
                    )
        else:
            recommendation = "hold"
            reason = (
                f"Humidité du sol {soil_moisture_pct:.1f} % ≥ "
                f"seuil critique {critical_threshold:.1f} %."
            )

    return {
        "recommendation": recommendation,
        "reason": reason,
        "soil_moisture_pct": soil_moisture_pct,
        "critical_moisture_threshold": _round(critical_threshold),
        "soil_moisture_status": _metric_status("soilMoisture", soil_moisture_pct),
        "et0_mm": et0_mm,
        "vpd_kpa": vpd_kpa,
        "dr_today_mm": dr_today_mm,
        "raw_mm": raw_mm,
        "zone_name": zone.name,
        "zone_area_m2": _round(zone.space),
        "estimated_water_m3": estimated_water_m3,
        "estimated_duration_min": estimated_duration_min,
        "morning_volume_m3": morning_volume_m3,
        "evening_volume_m3": evening_volume_m3,
        "decision_source": decision_source,
        "last_update_timestamp": (
            soil_reading.timestamp.isoformat()
            if soil_reading and soil_reading.timestamp
            else None
        ),
    }


def _list_recent_notifications(user, params: dict) -> dict:
    """The caller's recent irrigation-summary notifications, newest first."""
    from apps.alerts.models import Notification

    try:
        limit = int(params.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 50))

    rows = Notification.objects.filter(user=user).order_by("-notification_date")[:limit]
    notifications = []
    for n in rows:
        day = (
            n.last_irrigation_date.strftime("%Y-%m-%d")
            if n.last_irrigation_date
            else "—"
        )
        bits = []
        if n.ET0 is not None:
            bits.append(f"ET0 {n.ET0} mm")
        if n.soil_humidity is not None:
            bits.append(f"sol {n.soil_humidity} %")
        if n.soil_temperature is not None:
            bits.append(f"{n.soil_temperature} °C")
        if n.perfect_irrigation_period:
            bits.append(str(n.perfect_irrigation_period))
        notifications.append(
            {
                "id": n.id,
                "title": f"Irrigation {day}",
                "message": " · ".join(bits),
                "date": (
                    n.notification_date.isoformat() if n.notification_date else None
                ),
                "type": "irrigation_summary",
            }
        )
    return {"notifications": notifications}


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
registry.register(
    Tool(
        name="get_sensor_trend",
        description="Trend of one sensor over a rolling window: latest, min, max, "
        "average, sample count and direction (rising/falling/flat). Use to answer "
        '"how has X changed today / over the last N hours?".',
        handler=_get_sensor_trend,
        params={
            "sensor_key": {
                "type": "string",
                "required": True,
                "description": "Sensor to trend. One of: "
                + ", ".join(SENSOR_SOURCES.keys())
                + ".",
            },
            "zone_id": {
                "type": "integer",
                "required": False,
                "description": "Restrict the trend to a single zone.",
            },
            "hours": {
                "type": "integer",
                "required": False,
                "description": "Rolling window length in hours (default 24).",
            },
        },
    )
)
registry.register(
    Tool(
        name="get_irrigation_advice",
        description="Irrigation recommendation for a zone (irrigate / hold) with "
        "the reason, soil moisture vs critical threshold, ET0/VPD and an estimated "
        'water volume + duration. Use to answer "should I irrigate?".',
        handler=_get_irrigation_advice,
        params={
            "zone_id": {
                "type": "integer",
                "required": False,
                "description": "Zone to advise on (defaults to the user's first zone).",
            }
        },
    )
)
registry.register(
    Tool(
        name="list_recent_notifications",
        description="The caller's most recent irrigation-summary notifications, "
        "newest first.",
        handler=_list_recent_notifications,
        params={
            "limit": {
                "type": "integer",
                "required": False,
                "description": "Max notifications to return (default 5, max 50).",
            }
        },
    )
)
