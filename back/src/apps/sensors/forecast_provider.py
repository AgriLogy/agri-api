"""Daily-weather forecast provider for the ET₀ forecast (agrilogy-front #18).

Provides N days of daily weather aggregates that the agri-core ET₀-forecast
compute (:func:`agri.core.et_forecast.et0_forecast`) turns into a per-day ET₀.

Provider selection is env-driven (``ET_FORECAST_PROVIDER``):
  * ``mock`` (default) — a deterministic seasonal model keyed off latitude +
    day-of-year. No external calls, no API key, fully reproducible. This is
    what ships today; the forecast endpoint works out of the box.
  * ``openweather`` — REAL provider stub (One Call 3.0 daily shape), gated on
    ``WEATHER_API_KEY``. Intentionally left unimplemented so no secret or
    network dependency is introduced; wiring it is the documented follow-up.
    Falls back to the mock (logged) if selected without a key / before built.

Deterministic by design: given the same start date + latitude, the mock
returns the same series, so the endpoint + its tests are reproducible.
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

from agri.core.et_forecast import DailyWeatherForecast

logger = logging.getLogger(__name__)

# Open-Meteo — free, keyless forecast API. We read its own published FAO-56
# reference ET₀ (``et0_fao_evapotranspiration``) so the graph can show a real
# reference curve next to our computed bars. Best-effort: any failure yields an
# empty map and the curve is simply omitted (never fatal to the endpoint).
_OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
_OPENMETEO_TIMEOUT_S = float(os.getenv("OPENMETEO_TIMEOUT_S", "4.0"))


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


def _openweather_forecast(
    *, start: date, days: int, latitude: float | None, longitude: float | None
) -> list[DailyWeatherForecast]:
    """REAL provider stub. Not implemented — see module docstring.

    When built: call One Call 3.0 ``/onecall?...&exclude=current,minutely,hourly``
    with ``WEATHER_API_KEY``, map each ``daily[i]`` (temp.day, humidity,
    wind_speed, derive shortwave from ``uvi``/clouds, pressure) onto a
    ``DailyWeatherForecast``. Until then, fall back to the mock.
    """
    raise NotImplementedError("openweather forecast provider not yet wired")


def get_daily_forecast(
    *,
    start: date,
    days: int = 7,
    latitude: float | None = None,
    longitude: float | None = None,
) -> list[DailyWeatherForecast]:
    """Return ``days`` days of daily weather starting at ``start``.

    Selects the provider via ``ET_FORECAST_PROVIDER`` (default ``mock``);
    falls back to the mock if a real provider is selected but unavailable.
    """
    provider = (os.getenv("ET_FORECAST_PROVIDER", "mock") or "mock").strip().lower()
    if provider == "openweather":
        try:
            if not os.getenv("WEATHER_API_KEY"):
                raise NotImplementedError("WEATHER_API_KEY not set")
            return _openweather_forecast(
                start=start, days=days, latitude=latitude, longitude=longitude
            )
        except NotImplementedError as exc:
            logger.warning(
                "ET forecast: real provider unavailable (%s) — using mock", exc
            )
    return _mock_forecast(start=start, days=days, latitude=latitude)


def active_provider() -> str:
    """The provider name actually serving requests right now."""
    provider = (os.getenv("ET_FORECAST_PROVIDER", "mock") or "mock").strip().lower()
    if provider == "openweather" and os.getenv("WEATHER_API_KEY"):
        return "openweather"
    return "mock"


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
    if (os.getenv("ET0_OPENMETEO", "on") or "on").strip().lower() == "off":
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
