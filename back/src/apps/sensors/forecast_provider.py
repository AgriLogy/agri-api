"""Daily-weather forecast provider for the ET₀ forecast (agrilogy-front #18).

Provides N days of daily weather aggregates that the agri-core ET₀-forecast
compute (:func:`agri.core.et_forecast.et0_forecast`) turns into a per-day ET₀.

Provider selection is env-driven (``ET_FORECAST_PROVIDER``):
  * ``openmeteo`` (default) — REAL forecast from Open-Meteo (keyless). Hourly
    temperature / humidity / 10 m wind / shortwave / surface pressure are
    aggregated into the daily shape agri-core expects. Needs coordinates; the
    shared ``ET0_OPENMETEO=off`` switch disables it (tests / offline).
  * ``mock`` — a deterministic seasonal model keyed off latitude +
    day-of-year. No external calls, fully reproducible.
  * ``openweather`` — stub, not implemented; treated as unavailable.

Any real provider that is unavailable (no coordinates, network error, bad
payload, disabled) falls back to the mock, and :func:`get_daily_forecast_with_source`
reports which provider ACTUALLY served the data, so the UI label never claims
a real forecast for synthetic numbers.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from math import cos, pi, sin
from time import monotonic

from agri.core.agronomy import wind_speed_at_2m
from agri.core.et_forecast import DailyWeatherForecast

logger = logging.getLogger(__name__)

# Open-Meteo — free, keyless forecast API. We read its own published FAO-56
# reference ET₀ (``et0_fao_evapotranspiration``) so the graph can show a real
# reference curve next to our computed bars. Best-effort: any failure yields an
# empty map and the curve is simply omitted (never fatal to the endpoint).
_OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
_OPENMETEO_TIMEOUT_S = float(os.getenv("OPENMETEO_TIMEOUT_S", "4.0"))

_OPENMETEO_HOURLY = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "shortwave_radiation",
    "surface_pressure",
)
# agri-core's diurnal model spreads ``rs_wm2`` as a half-sine over a fixed
# 06:00–18:00 day, so its daily energy is peak × 12 h × 2/π. We invert that so
# the synthesised day carries the same shortwave energy Open-Meteo forecasts.
_DAYLIGHT_H = 12.0
# A forecast changes a few times a day; cache per (coords, window) so dashboard
# refreshes don't re-hit Open-Meteo. In-process, bounded, short TTL.
_FORECAST_TTL_S = float(os.getenv("OPENMETEO_FORECAST_TTL_S", "1800"))
_FORECAST_CACHE_MAX = 256
_FORECAST_CACHE: dict[tuple, tuple[float, list[DailyWeatherForecast]]] = {}

PROVIDER_MOCK = "mock"
PROVIDER_OPENMETEO = "open-meteo"


def _seasonal_mock_day(day: date, latitude: float | None) -> DailyWeatherForecast:
    """A plausible, deterministic daily-weather record for ``day``.

    Seasonal swing follows day-of-year (warmer/brighter near the summer
    solstice), hemisphere-aware via ``latitude``. A small day-to-day wiggle
    keyed off the ordinal keeps consecutive days from being identical without
    introducing randomness.
    """
    doy = day.timetuple().tm_yday
    lat = 33.0 if latitude is None else latitude
    # Phase so the northern hemisphere peaks ~late June (doy ~172); flip south.
    season = cos(2.0 * pi * (doy - 172) / 365.0)
    if lat < 0:
        season = -season
    wiggle = sin(day.toordinal() % 7)  # deterministic, ±~1

    temp_c = 20.0 + 8.0 * season + 1.5 * wiggle
    rs_peak_wm2 = 650.0 + 250.0 * max(0.0, season) + 30.0 * wiggle
    rh_pct = max(20.0, min(90.0, 55.0 - 12.0 * season - 4.0 * wiggle))
    wind_ms = 2.0 + 0.6 * abs(wiggle)
    pressure_hpa = 1013.0

    return DailyWeatherForecast(
        day=day,
        temp_c=round(temp_c, 1),
        rh_pct=round(rh_pct, 1),
        wind_ms=round(wind_ms, 1),
        rs_wm2=round(rs_peak_wm2, 1),
        pressure_hpa=pressure_hpa,
    )


def _mock_forecast(
    *, start: date, days: int, latitude: float | None
) -> list[DailyWeatherForecast]:
    return [
        _seasonal_mock_day(start + timedelta(days=i), latitude) for i in range(days)
    ]


class ProviderUnavailable(Exception):
    """A real provider can't serve this request — caller falls back to mock."""


def _openmeteo_disabled() -> bool:
    return (os.getenv("ET0_OPENMETEO", "on") or "on").strip().lower() == "off"


def _mean(values: list) -> float | None:
    nums = [float(v) for v in values if v is not None]
    return sum(nums) / len(nums) if nums else None


def _openmeteo_forecast(
    *, start: date, days: int, latitude: float | None, longitude: float | None
) -> list[DailyWeatherForecast]:
    """Daily weather for ``days`` days from Open-Meteo's hourly forecast.

    Hourly series are grouped by UTC date and reduced to agri-core's
    :class:`DailyWeatherForecast`: mean temperature / RH / surface pressure,
    mean wind projected from 10 m to 2 m (FAO-56 eq. 47), and the peak
    shortwave that reproduces the day's forecast radiation energy.

    Raises :class:`ProviderUnavailable` on missing coordinates, network /
    parse errors, or any day without usable data — partial real data is not
    mixed with synthetic days.
    """
    if _openmeteo_disabled():
        raise ProviderUnavailable("ET0_OPENMETEO=off")
    if latitude is None or longitude is None:
        raise ProviderUnavailable("no coordinates")

    lat, lon = round(float(latitude), 4), round(float(longitude), 4)
    key = (lat, lon, start.isoformat(), days)
    hit = _FORECAST_CACHE.get(key)
    if hit is not None and monotonic() - hit[0] < _FORECAST_TTL_S:
        return list(hit[1])

    end = start + timedelta(days=max(1, days) - 1)
    query = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(_OPENMETEO_HOURLY),
            "wind_speed_unit": "ms",
            "timezone": "UTC",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
    )
    try:
        req = urllib.request.Request(
            f"{_OPENMETEO_URL}?{query}",
            headers={"User-Agent": "agri-api/et0-forecast"},
        )
        with urllib.request.urlopen(req, timeout=_OPENMETEO_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        hourly = payload["hourly"]
        times: list[str] = hourly["time"]
        series = {name: hourly[name] for name in _OPENMETEO_HOURLY}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise ProviderUnavailable(f"fetch failed: {exc}") from exc
    except (KeyError, TypeError) as exc:
        raise ProviderUnavailable(f"unexpected payload: {exc!r}") from exc

    by_day: dict[str, list[int]] = {}
    for i, ts in enumerate(times):
        by_day.setdefault(str(ts)[:10], []).append(i)

    out: list[DailyWeatherForecast] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        idx = by_day.get(day.isoformat())
        if not idx:
            raise ProviderUnavailable(f"no hourly data for {day}")
        col = {
            name: [series[name][i] for i in idx if i < len(series[name])]
            for name in _OPENMETEO_HOURLY
        }
        temp_c = _mean(col["temperature_2m"])
        rh_pct = _mean(col["relative_humidity_2m"])
        wind10 = _mean(col["wind_speed_10m"])
        rs_mean = _mean(col["shortwave_radiation"])
        pressure = _mean(col["surface_pressure"])
        if None in (temp_c, rh_pct, wind10, rs_mean):
            raise ProviderUnavailable(f"incomplete hourly data for {day}")
        # Mean W/m² over 24 h → daily Wh/m² → half-sine peak over the daylight.
        rs_peak = rs_mean * 24.0 * pi / (2.0 * _DAYLIGHT_H)
        out.append(
            DailyWeatherForecast(
                day=day,
                temp_c=round(temp_c, 1),
                rh_pct=round(max(1.0, min(100.0, rh_pct)), 1),
                wind_ms=round(wind_speed_at_2m(wind10, 10.0), 2),
                rs_wm2=round(rs_peak, 1),
                pressure_hpa=round(pressure, 1) if pressure is not None else 1013.0,
            )
        )

    if len(_FORECAST_CACHE) >= _FORECAST_CACHE_MAX:
        _FORECAST_CACHE.clear()
    _FORECAST_CACHE[key] = (monotonic(), out)
    return list(out)


def _configured_provider() -> str:
    raw = os.getenv("ET_FORECAST_PROVIDER", "openmeteo") or "openmeteo"
    return raw.strip().lower().replace("-", "").replace("_", "")


def get_daily_forecast_with_source(
    *,
    start: date,
    days: int = 7,
    latitude: float | None = None,
    longitude: float | None = None,
) -> tuple[list[DailyWeatherForecast], str]:
    """``(daily weather, provider that actually served it)``.

    The provider is ``"open-meteo"`` only when real data came back; any
    fallback reports ``"mock"``.
    """
    provider = _configured_provider()
    if provider == "openmeteo":
        try:
            return (
                _openmeteo_forecast(
                    start=start, days=days, latitude=latitude, longitude=longitude
                ),
                PROVIDER_OPENMETEO,
            )
        except ProviderUnavailable as exc:
            logger.warning("ET forecast: Open-Meteo unavailable (%s) — using mock", exc)
    elif provider != "mock":
        logger.warning(
            "ET forecast: provider %r not implemented — using mock", provider
        )
    return _mock_forecast(start=start, days=days, latitude=latitude), PROVIDER_MOCK


def get_daily_forecast(
    *,
    start: date,
    days: int = 7,
    latitude: float | None = None,
    longitude: float | None = None,
) -> list[DailyWeatherForecast]:
    """Return ``days`` days of daily weather starting at ``start``.

    Kept for callers that don't need the source; prefer
    :func:`get_daily_forecast_with_source` so the label stays truthful.
    """
    daily, _ = get_daily_forecast_with_source(
        start=start, days=days, latitude=latitude, longitude=longitude
    )
    return daily


def fetch_openmeteo_et0(
    *,
    start: date,
    days: int,
    latitude: float | None,
    longitude: float | None,
) -> dict[str, float]:
    """Return ``{iso_date: et0_mm}`` from Open-Meteo's published FAO-56 reference
    ET₀, for the ``days`` days starting at ``start``.

    Keyless and best-effort: returns ``{}`` when lat/lon are missing or on any
    network / timeout / parse error. The ET₀ graph uses this as an optional real
    reference curve, so an empty map just means "no curve this request" — it is
    never allowed to fail the forecast endpoint. Set ``ET0_OPENMETEO=off`` to
    disable the call entirely (e.g. in tests / offline).
    """
    if _openmeteo_disabled():
        return {}
    if latitude is None or longitude is None:
        return {}

    end = start + timedelta(days=max(1, days) - 1)
    query = urllib.parse.urlencode(
        {
            "latitude": round(float(latitude), 4),
            "longitude": round(float(longitude), 4),
            "daily": "et0_fao_evapotranspiration",
            "timezone": "UTC",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
    )
    url = f"{_OPENMETEO_URL}?{query}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "agri-api/et0-forecast"}
        )
        with urllib.request.urlopen(req, timeout=_OPENMETEO_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        daily = payload.get("daily") or {}
        dates = daily.get("time") or []
        values = daily.get("et0_fao_evapotranspiration") or []
        out: dict[str, float] = {}
        for iso, value in zip(dates, values):
            if value is None:
                continue
            out[iso] = round(float(value), 2)
        return out
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning(
            "Open-Meteo ET₀ fetch failed (%s) — reference curve omitted", exc
        )
        return {}
