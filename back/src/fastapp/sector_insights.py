"""Farm-map decision layer — data gathering for agri.core.sector_insights.

agri-api#459 phase 3. The maths lives in agri-core (pure); this module reads
what it needs for one sector and calls it:

* the sector's zones → soil water parameters (TAW / RAW), area, pump flow,
  daily water cap and critical moisture threshold;
* the farmer's Kc table attached to one of those zones;
* past and coming daily ET₀ + rain at the sector centroid (Open-Meteo);
* metered irrigation (``water_flow``, m³/h) integrated per day;
* the latest soil moisture, calibrated like the dashboard shows it;
* stored NDVI for the sector's current shape (no Copernicus call here).

Everything is owner-scoped by the caller. Dates are UTC days, matching the
Open-Meteo request.

Human strings come in French, Arabic or English (``lang``); agri-core owns the
advice and vegetation wording, the water-balance assumption is worded here.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from agri.core.et_forecast import et0_forecast
from agri.core.geometry import centroid
from agri.core.sector_insights import (
    BalanceDay,
    KcPeriod,
    balance_status,
    interpret_vegetation,
    irrigation_depth_mm,
    kc_for_day,
    normalize_lang,
    recommend,
    run_water_balance,
)
from agri.db.analytics import (
    AnalyticsKc,
    AnalyticsKcperiod,
    AnalyticsKcperiodassignment,
    AnalyticsSector,
    AnalyticsSoilmoisturemedium,
    AnalyticsWaterflowsensor,
    AnalyticsZone,
)
from apps.sensors.forecast_provider import (
    fetch_openmeteo_daily_water,
    get_daily_forecast_with_source,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fastapp.calibration import corrected_value, load_calibrations
from fastapp.farm_map_tables import sector_meta
from fastapp.satellite import store as satellite_store
from fastapp.schema_compat import sector_geometry_available

BALANCE_DAYS = 14
"""Water-balance window: Dr is assumed at field capacity at its start."""

NDVI_LOOKBACK_DAYS = 60
SOIL_MOISTURE_MAX_AGE = dt.timedelta(hours=72)
"""A probe reading older than this is not "the soil now": ignore it (a farm
whose probe went silent in June must not be read with June's moisture)."""
SOIL_MOISTURE_KEY = "soil_moisture_medium"


INSIGHT_LANGS = ("fr", "ar", "en")
DEFAULT_LANG = "fr"

_EN_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip

_ASSUMPTION = {
    "fr": "Sol supposé à la capacité au champ le {date} (début du bilan sur {days} jours).",
    "en": "Soil assumed at field capacity on {date} (start of the {days}-day balance).",
    "ar": "يُفترض أن التربة عند السعة الحقلية بتاريخ {date} (بداية الميزان المائي لمدة {days} يومًا).",
}


def resolve_lang(requested: str | None, preferred: str | None) -> str:
    """The language of the insight strings.

    ``?lang=`` wins when given (an unknown value falls back to French, not to
    the user's preference); without it, the caller's ``preferred_language``,
    else French.
    """
    if requested is not None and requested.strip():
        return normalize_lang(requested)
    if preferred and preferred.strip().lower() in INSIGHT_LANGS:
        return preferred.strip().lower()
    return DEFAULT_LANG


def _assumption(start: dt.date, lang: str) -> str:
    if lang == "en":
        day = f"{start.day} {_EN_MONTHS[start.month - 1]} {start.year}"
    else:
        day = start.strftime("%d/%m/%Y")
    return _ASSUMPTION.get(lang, _ASSUMPTION[DEFAULT_LANG]).format(
        date=day, days=BALANCE_DAYS
    )


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# ------------------------------------------------------------------ sector
def load_sector(
    session: Session, sector_id: int, user_id: int
) -> dict[str, Any] | None:
    """The sector's name, shape and crop through the Core mirror, or None."""
    name = session.scalar(
        select(AnalyticsSector.name).where(
            AnalyticsSector.id == sector_id, AnalyticsSector.user_id == user_id
        )
    )
    if name is None:
        return None
    data: dict[str, Any] = {"id": sector_id, "name": name}
    if sector_geometry_available(session):
        row = session.execute(
            select(
                sector_meta.c.geometry, sector_meta.c.area_ha, sector_meta.c.crop
            ).where(sector_meta.c.id == sector_id)
        ).first()
        if row is not None:
            data.update(row._mapping)
    data.setdefault("geometry", None)
    data.setdefault("area_ha", None)
    data.setdefault("crop", None)
    return data


def sector_coordinates(
    geometry: dict[str, Any] | None, zones: list[AnalyticsZone]
) -> tuple[float, float] | None:
    """``(lat, lon)``: the shape's centroid, else a zone's weather station."""
    if isinstance(geometry, dict):
        try:
            lon, lat = centroid(geometry)
            return lat, lon
        except (ValueError, TypeError):
            pass
    for zone in zones:
        lat = getattr(zone, "humidity_weather_latitude", None)
        lon = getattr(zone, "humidity_weather_longitude", None)
        if lat and lon:
            return float(lat), float(lon)
    return None


def _zones(
    session: Session,
    sector_id: int,
    user_id: int,
    zone_ids: set[int] | None = None,
) -> list[AnalyticsZone]:
    query = select(AnalyticsZone).where(
        AnalyticsZone.sector_id == sector_id, AnalyticsZone.user_id == user_id
    )
    if zone_ids is not None:  # a technician: only the granted zones
        query = query.where(AnalyticsZone.id.in_(sorted(zone_ids)))
    return list(session.scalars(query.order_by(AnalyticsZone.id)))


def _kc_periods(session: Session, zone_ids: list[int]) -> list[KcPeriod]:
    if not zone_ids:
        return []
    kc = session.scalars(
        select(AnalyticsKc)
        .where(AnalyticsKc.zone_id.in_(zone_ids))
        .order_by(AnalyticsKc.id)
    ).first()
    if kc is None:
        return []
    rows = session.scalars(
        select(AnalyticsKcperiod)
        .join(
            AnalyticsKcperiodassignment,
            AnalyticsKcperiodassignment.period_id == AnalyticsKcperiod.id,
        )
        .where(AnalyticsKcperiodassignment.kc_id == kc.id)
    )
    return [KcPeriod(p.period_name, p.start_date, p.end_date, p.kc_value) for p in rows]


def _daily_irrigation_m3(
    session: Session, zone_ids: list[int], start: dt.date, end: dt.date
) -> dict[dt.date, float]:
    """Metered volume per UTC day: hourly mean flow (m³/h) × 1 h, summed."""
    if not zone_ids:
        return {}
    hour = func.date_trunc("hour", AnalyticsWaterflowsensor.timestamp)
    lo = dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC)
    hi = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
    rows = session.execute(
        select(hour.label("h"), func.avg(AnalyticsWaterflowsensor.value))
        .where(
            AnalyticsWaterflowsensor.zone_id.in_(zone_ids),
            AnalyticsWaterflowsensor.timestamp >= lo,
            AnalyticsWaterflowsensor.timestamp < hi,
            AnalyticsWaterflowsensor.value > 0,
        )
        .group_by(AnalyticsWaterflowsensor.zone_id, hour)
    ).all()
    out: dict[dt.date, float] = defaultdict(float)
    for h, flow in rows:
        if flow is not None:
            out[h.astimezone(dt.UTC).date()] += float(flow)
    return dict(out)


def _early_season(source: str, period_name: str | None, kc: float) -> bool:
    """The farmer's Kc calendar says the crop is at its initial stage: a named
    "Initial…" period, or a period Kc at or under 0.5 (FAO-56 Kc ini range)."""
    if source != "kc_period":
        return False
    name = (period_name or "").strip().lower()
    return name.startswith(("init", "semis", "levée", "levee")) or kc <= 0.5


def _soil_moisture(
    session: Session, zone_ids: list[int], *, now: dt.datetime | None = None
) -> tuple[float | None, str | None]:
    if not zone_ids:
        return None, None
    since = (now or dt.datetime.now(dt.UTC)) - SOIL_MOISTURE_MAX_AGE
    row = session.scalars(
        select(AnalyticsSoilmoisturemedium)
        .where(
            AnalyticsSoilmoisturemedium.zone_id.in_(zone_ids),
            AnalyticsSoilmoisturemedium.timestamp >= since,
        )
        .order_by(AnalyticsSoilmoisturemedium.timestamp.desc())
        .limit(1)
    ).first()
    if row is None or row.value is None:
        return None, None
    device_id = getattr(row, "device_id", None)
    cal = load_calibrations(session, [(device_id, SOIL_MOISTURE_KEY)]).get(
        (device_id, SOIL_MOISTURE_KEY)
    )
    value = corrected_value(
        row.value, cal, sensor_key=SOIL_MOISTURE_KEY, native_unit="%"
    )
    return (
        round(float(value), 1) if value is not None else None
    ), row.timestamp.isoformat()


# ---------------------------------------------------------------- insights
def sector_insights(
    session: Session,
    sector_id: int,
    user_id: int,
    *,
    today: dt.date | None = None,
    zone_ids: set[int] | None = None,
    lang: str = DEFAULT_LANG,
) -> dict[str, Any] | None:
    """The ``GET /farm-map/sectors/{id}/insights`` body, or None (not found).

    ``user_id`` is the data owner; ``zone_ids`` (a technician's granted zones)
    narrows the zones the balance is computed on, None meaning all of them.
    ``lang`` (fr | ar | en) picks the language of the human strings only.
    """
    lang = normalize_lang(lang)
    sector = load_sector(session, sector_id, user_id)
    if sector is None:
        return None
    today = today or _today()
    zones = _zones(session, sector_id, user_id, zone_ids)
    zone_ids = [z.id for z in zones]

    periods = _kc_periods(session, zone_ids)
    kc_today, kc_source, kc_name = kc_for_day(periods, today)

    soil_zone = next(
        (
            z
            for z in zones
            if _positive(z.soil_param_TAW) and _positive(z.soil_param_RAW)
        ),
        None,
    )
    critical = next(
        (
            _positive(z.critical_moisture_threshold)
            for z in zones
            if z.critical_moisture_threshold
        ),
        None,
    )
    area_m2 = (_positive(sector.get("area_ha")) or 0.0) * 10_000.0 or (
        sum(_positive(z.space) or 0.0 for z in zones) or None
    )
    pump_l_s = sum(_positive(z.pomp_flow_rate) or 0.0 for z in zones) or None
    max_day_l = (
        sum(_positive(z.irrigation_water_quantity) or 0.0 for z in zones) or None
    )

    coords = sector_coordinates(sector.get("geometry"), zones)
    start = today - dt.timedelta(days=BALANCE_DAYS - 1)
    weather: dict[str, dict[str, float | None]] = {}
    if coords is not None:
        weather = fetch_openmeteo_daily_water(
            start=start,
            end=today + dt.timedelta(days=2),
            latitude=round(coords[0], 2),
            longitude=round(coords[1], 2),
        )

    soil_pct, soil_at = _soil_moisture(session, zone_ids)

    reason = None
    if not zones:
        reason = "no_zone"
    elif soil_zone is None:
        reason = "no_soil_params"
    elif coords is None:
        reason = "no_location"
    elif not any(
        (weather.get((start + dt.timedelta(days=i)).isoformat()) or {}).get("et0_mm")
        is not None
        for i in range(BALANCE_DAYS)
    ):
        reason = "no_weather"

    water_balance: dict[str, Any] = {"available": reason is None, "reason": reason}
    recommendation = None
    depletion_ratio = None
    if reason is None:
        taw = float(soil_zone.soil_param_TAW)
        raw = float(soil_zone.soil_param_RAW)
        irrigation = _daily_irrigation_m3(session, zone_ids, start, today)
        days = []
        for i in range(BALANCE_DAYS):
            day = start + dt.timedelta(days=i)
            w = weather.get(day.isoformat()) or {}
            days.append(
                BalanceDay(
                    day=day,
                    et0_mm=w.get("et0_mm"),
                    rain_mm=w.get("rain_mm") or 0.0,
                    irrigation_mm=irrigation_depth_mm(
                        irrigation.get(day, 0.0) * 1000.0, area_m2
                    ),
                    kc=kc_for_day(periods, day)[0],
                )
            )
        rows = run_water_balance(days, taw_mm=taw)
        dr = rows[-1].dr_mm
        depletion_ratio = dr / raw if raw else None
        water_balance.update(
            {
                "taw_mm": round(taw, 1),
                "raw_mm": round(raw, 1),
                "dr_mm": round(dr, 1),
                "depletion_pct": round(dr / taw * 100.0, 1) if taw else None,
                "status": balance_status(dr, raw),
                "start_date": start.isoformat(),
                "assumption": _assumption(start, lang),
                "days": [r.as_dict() for r in rows],
            }
        )
        rain_48h = sum(
            (weather.get((today + dt.timedelta(days=i)).isoformat()) or {}).get(
                "rain_mm"
            )
            or 0.0
            for i in (1, 2)
        )
        recommendation = recommend(
            dr_mm=dr,
            raw_mm=raw,
            soil_moisture_pct=soil_pct,
            critical_moisture_pct=critical,
            area_m2=area_m2,
            pump_flow_l_per_s=pump_l_s,
            max_water_per_day_l=max_day_l,
            rain_next_48h_mm=rain_48h,
            lang=lang,
        )

    ndvi_points: list[tuple[dt.date, float]] = []
    if isinstance(sector.get("geometry"), dict):
        series = satellite_store.stored_series(
            session,
            sector_id,
            sector["geometry"],
            "ndvi",
            today - dt.timedelta(days=NDVI_LOOKBACK_DAYS),
            today,
        )
        ndvi_points = [
            (dt.date.fromisoformat(p["date"]), float(p["mean"])) for p in series
        ]
    vegetation = interpret_vegetation(
        ndvi_points=ndvi_points,
        soil_moisture_pct=soil_pct,
        critical_moisture_pct=critical,
        depletion_ratio=depletion_ratio,
        today=today,
        lang=lang,
        early_season=_early_season(kc_source, kc_name, kc_today),
    )
    vegetation["soil_moisture_pct"] = soil_pct
    vegetation["soil_moisture_at"] = soil_at

    return {
        "sector_id": sector_id,
        "lang": lang,
        "sector_name": sector["name"],
        "date": today.isoformat(),
        "crop": sector.get("crop"),
        "zone_ids": zone_ids,
        "kc": {"value": kc_today, "source": kc_source, "period_name": kc_name},
        "water_balance": water_balance,
        "recommendation": recommendation,
        "vegetation": vegetation,
    }


# ---------------------------------------------------------------- forecast
def sectors_forecast(
    session: Session,
    user_id: int,
    *,
    days: int,
    today: dt.date | None = None,
    sector_ids: list[int] | None = None,
) -> dict[str, Any]:
    """The ``GET /farm-map/forecast`` body: rain + ET₀ per drawn sector.

    ``user_id`` is the data owner; ``sector_ids`` (a technician's readable
    sectors) narrows the list, None meaning all of the owner's sectors.
    """
    today = today or _today()
    dates = [(today + dt.timedelta(days=i)).isoformat() for i in range(days)]
    if not sector_geometry_available(session):
        return {"provider": None, "days": dates, "sectors": []}
    query = select(sector_meta.c.id, sector_meta.c.geometry).where(
        sector_meta.c.user_id == user_id, sector_meta.c.geometry.is_not(None)
    )
    if sector_ids is not None:
        query = query.where(sector_meta.c.id.in_(sector_ids))
    rows = session.execute(query.order_by(sector_meta.c.id)).all()
    # The Core mirror has no name column; read names through the ORM model.
    names = dict(
        session.execute(
            select(AnalyticsSector.id, AnalyticsSector.name).where(
                AnalyticsSector.user_id == user_id
            )
        ).all()
    )

    by_coords: dict[tuple[float, float], tuple[list[dict[str, Any]], str]] = {}
    providers: set[str] = set()
    sectors = []
    for sid, geometry in rows:
        name = names.get(sid, "")
        coords = sector_coordinates(geometry, [])
        if coords is None:
            continue
        lat, lon = coords
        key = (round(lat, 2), round(lon, 2))  # ~1 km: one weather call per farm
        if key not in by_coords:
            daily, provider = get_daily_forecast_with_source(
                start=today, days=days, latitude=key[0], longitude=key[1]
            )
            by_coords[key] = (
                et0_forecast(daily, latitude=key[0], longitude=key[1]),
                provider,
            )
        forecast, provider = by_coords[key]
        providers.add(provider)
        daily_rows = [
            {"date": d["date"], "rain_mm": d.get("rain_mm", 0.0), "et0_mm": d["et0_mm"]}
            for d in forecast
        ]
        rain = round(sum(d["rain_mm"] or 0.0 for d in daily_rows), 1)
        et0 = round(sum(d["et0_mm"] or 0.0 for d in daily_rows), 1)
        sectors.append(
            {
                "sector_id": sid,
                "name": name,
                "centroid": {"lat": round(lat, 5), "lon": round(lon, 5)},
                "daily": daily_rows,
                "total_rain_mm": rain,
                "total_et0_mm": et0,
                "balance_mm": round(rain - et0, 1),
            }
        )
    provider = (
        providers.pop() if len(providers) == 1 else ("mixed" if providers else None)
    )
    return {"provider": provider, "days": dates, "sectors": sectors}
