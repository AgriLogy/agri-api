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
# Optional series: a payload without it still yields a forecast (rain = 0).
_OPENMETEO_HOURLY_OPTIONAL = ("precipitation",)
# agri-core's diurnal model spreads ``rs_wm2`` as a half-sine over a fixed
# 06:00–18:00 day, so its daily energy is peak × 12 h × 2/π. We invert that so
# the synthesised day carries the same shortwave energy Open-Meteo forecasts.
_DAYLIGHT_H = 12.0
# A forecast changes a few times a day; cache per (coords, window) so dashboard
# refreshes don't re-hit Open-Meteo. In-process, bounded, short TTL.
_FORECAST_TTL_S = float(os.getenv("OPENMETEO_FORECAST_TTL_S", "1800"))
_FORECAST_CACHE_MAX = 256
_FORECAST_CACHE: dict[tuple, tuple[float, list[DailyWeatherForecast]]] = {}
# When Open-Meteo fails (it occasionally times out from the droplet), a cached
# answer for the same spot and window younger than this is served instead of
# nothing: yesterday-old weather beats "weather unavailable".
_STALE_MAX_S = float(os.getenv("OPENMETEO_STALE_MAX_S", "86400"))
_FETCH_ATTEMPTS = 2  # one retry, on network errors only


def _fetch_json(url: str, user_agent: str) -> dict:
    """GET ``url`` as JSON, retrying once on a network error or timeout."""
    last: Exception | None = None
    for _ in range(_FETCH_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=_OPENMETEO_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
    assert last is not None
    raise last


def _stale(cache: dict, key: tuple):
    """A cached value for ``key`` past its TTL but within the stale window."""
    hit = cache.get(key)
    if hit is not None and monotonic() - hit[0] < _STALE_MAX_S:
        return hit[1]
    return None


# The in-memory stale tier dies with the process, so a deploy during an
# Open-Meteo outage left nothing to serve (#518). The last good answer is also
# kept in Redis (the Celery broker) for the stale window. Best-effort only:
# without a URL, or on any Redis error, it is skipped.
_PERSIST_PREFIX = "openmeteo:last-good:"
_redis_client = None  # lazily built; tests swap in a fake


def _redis():
    global _redis_client
    if _redis_client is None:
        url = os.getenv("OPENMETEO_STALE_REDIS_URL") or os.getenv("CELERY_BROKER_URL")
        if not url:
            return None
        import redis

        _redis_client = redis.from_url(
            url, socket_connect_timeout=0.5, socket_timeout=0.5
        )
    return _redis_client


def _persist_key(kind: str, key: tuple) -> str:
    return _PERSIST_PREFIX + kind + ":" + "|".join(str(part) for part in key)


def _persist_set(kind: str, key: tuple, value) -> None:
    try:
        client = _redis()
        if client is not None:
            client.set(_persist_key(kind, key), json.dumps(value), ex=int(_STALE_MAX_S))
    except Exception as exc:  # Redis down never breaks a weather call
        logger.debug("Open-Meteo last-good write skipped (%s)", exc)


def _persist_get(kind: str, key: tuple):
    try:
        client = _redis()
        raw = client.get(_persist_key(kind, key)) if client is not None else None
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.debug("Open-Meteo last-good read skipped (%s)", exc)
        return None


def _forecast_to_json(days: list[DailyWeatherForecast]) -> list[dict]:
    return [
        {
            "day": d.day.isoformat(),
            "temp_c": d.temp_c,
            "rh_pct": d.rh_pct,
            "wind_ms": d.wind_ms,
            "rs_wm2": d.rs_wm2,
            "pressure_hpa": d.pressure_hpa,
            "precipitation_mm": d.precipitation_mm,
        }
        for d in days
    ]


def _forecast_from_json(rows) -> list[DailyWeatherForecast] | None:
    try:
        return [
            DailyWeatherForecast(**{**row, "day": date.fromisoformat(row["day"])})
            for row in rows
        ]
    except (KeyError, TypeError, ValueError):
        return None


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
    # Deterministic showers, wetter in the cool season (Mediterranean winter).
    rain_mm = max(0.0, 6.0 * wiggle - 3.0) * (1.0 if season < 0.3 else 0.3)

    return DailyWeatherForecast(
        day=day,
        temp_c=round(temp_c, 1),
        rh_pct=round(rh_pct, 1),
        wind_ms=round(wind_ms, 1),
        rs_wm2=round(rs_peak_wm2, 1),
        pressure_hpa=pressure_hpa,
        precipitation_mm=round(rain_mm, 1),
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
            "hourly": ",".join(_OPENMETEO_HOURLY + _OPENMETEO_HOURLY_OPTIONAL),
            "wind_speed_unit": "ms",
            "timezone": "UTC",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
    )
    try:
        payload = _fetch_json(f"{_OPENMETEO_URL}?{query}", "agri-api/et0-forecast")
        hourly = payload["hourly"]
        times: list[str] = hourly["time"]
        series = {name: hourly[name] for name in _OPENMETEO_HOURLY}
        rain_series = hourly.get("precipitation") or []
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        stale = _stale(_FORECAST_CACHE, key)
        if stale is None:
            stale = _forecast_from_json(_persist_get("forecast", key) or [])
        if stale:
            logger.warning("Open-Meteo forecast failed (%s) — serving cached data", exc)
            return list(stale)
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
        rain = sum(
            float(rain_series[i])
            for i in idx
            if i < len(rain_series) and rain_series[i] is not None
        )
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
                precipitation_mm=round(rain, 1),
            )
        )

    if len(_FORECAST_CACHE) >= _FORECAST_CACHE_MAX:
        _FORECAST_CACHE.clear()
    _FORECAST_CACHE[key] = (monotonic(), out)
    _persist_set("forecast", key, _forecast_to_json(out))
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


_WATER_CACHE: dict[tuple, tuple[float, dict[str, dict[str, float | None]]]] = {}


def fetch_openmeteo_daily_water(
    *,
    start: date,
    end: date,
    latitude: float | None,
    longitude: float | None,
) -> dict[str, dict[str, float | None]]:
    """``{iso_date: {"et0_mm": …, "rain_mm": …}}`` from Open-Meteo, past or future.

    Open-Meteo's forecast endpoint serves roughly the last three months as
    well as the coming days, which covers the sector water balance (two weeks
    back) and the forecast layer (one week ahead). Best-effort like
    :func:`fetch_openmeteo_et0`: ``{}`` when disabled, without coordinates or on
    any network / parse error. Cached per (coords, window) for the forecast TTL.
    """
    if _openmeteo_disabled() or latitude is None or longitude is None:
        return {}
    lat, lon = round(float(latitude), 3), round(float(longitude), 3)
    key = (lat, lon, start.isoformat(), end.isoformat())
    hit = _WATER_CACHE.get(key)
    if hit is not None and monotonic() - hit[0] < _FORECAST_TTL_S:
        return dict(hit[1])
    query = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "daily": "et0_fao_evapotranspiration,precipitation_sum",
            "timezone": "UTC",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
    )
    try:
        payload = _fetch_json(f"{_OPENMETEO_URL}?{query}", "agri-api/sector-water")
        daily = payload.get("daily") or {}
        dates = daily.get("time") or []
        et0 = daily.get("et0_fao_evapotranspiration") or []
        rain = daily.get("precipitation_sum") or []
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        stale = _stale(_WATER_CACHE, key)
        if stale is None:
            stale = _persist_get("water", key)
        if stale:
            logger.warning(
                "Open-Meteo daily water failed (%s) — serving cached data", exc
            )
            return dict(stale)
        logger.warning("Open-Meteo daily water fetch failed (%s)", exc)
        return {}

    def at(values: list, i: int) -> float | None:
        if i < len(values) and values[i] is not None:
            return round(float(values[i]), 2)
        return None

    out = {
        iso: {"et0_mm": at(et0, i), "rain_mm": at(rain, i)}
        for i, iso in enumerate(dates)
    }
    if len(_WATER_CACHE) >= _FORECAST_CACHE_MAX:
        _WATER_CACHE.clear()
    _WATER_CACHE[key] = (monotonic(), out)
    if out:
        _persist_set("water", key, out)
    return dict(out)


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
