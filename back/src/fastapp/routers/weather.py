"""fastapp /weather — reference-ET₀ forecast.

Strangler port of ``apps/sensors/router_et_forecast.py`` (django-ninja).
Byte-parity with the Django version: same route, same query params + clamp,
same 404 shape (``{"detail": "Zone not found."}``), same response body
(``{"zone_id", "provider", "days": [{"date", "et0_mm"}]}``).

Read-only + owner-scoped. Data access is SQLAlchemy via agri-core's session
(no Django ORM): the zone + the caller's lat/lon come from ``agri.db``. The
forecast provider (``apps.sensors.forecast_provider``) is framework-agnostic
today (stdlib + agri.core only) — it moves into agri-core in a later phase;
imported directly here so this cutover needs no cross-repo release.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException

from agri.core.database.session import session_scope
from agri.core.et_forecast import et0_forecast
from agri.db.analytics import AnalyticsZone
from agri.db.users import CustomUserCustomuser
from apps.sensors.forecast_provider import (
    active_provider,
    fetch_openmeteo_et0,
    get_daily_forecast,
)
from fastapp.auth import AuthedUser, get_current_user

router = APIRouter(tags=["weather"])

_MAX_DAYS = 14
# Cap the comparison-series range so a wide date picker can't fan out into a
# huge Open-Meteo request (its forecast window is limited anyway).
_MAX_SERIES_DAYS = 31


def _parse_iso_date(value: str | None) -> datetime.date | None:
    """'YYYY-MM-DD' → date, anything else → None (matches the sensors router)."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _pick_coords(
    *candidates: tuple[float | None, float | None],
) -> tuple[float | None, float | None]:
    """First (lat, lon) pair where both are set, else (None, None).

    Used to source Open-Meteo coordinates: the farmer's picked weather location
    (passed by the frontend) wins, then the account's lat/lon, then the zone's
    stored weather coordinates. Most farmer accounts have no server-side lat/lon
    (the picker is client-side), so the zone fallback keeps the curve populated.
    """
    for lat, lon in candidates:
        if lat is not None and lon is not None:
            return lat, lon
    return None, None


@router.get(
    "/weather/et-forecast",
    summary="Daily reference-ET0 forecast for one of the caller's zones",
)
def et_forecast(
    zone_id: int,
    days: int = 7,
    lat: float | None = None,
    lon: float | None = None,
    user: AuthedUser = Depends(get_current_user),
):
    days = max(1, min(_MAX_DAYS, days))

    with session_scope() as session:
        zone = session.get(AnalyticsZone, zone_id)
        # Owner-scoped: a zone the caller doesn't own is indistinguishable
        # from a missing one (same 404, no ownership leak) — matches the
        # Django ``filter(id=..., user=request.auth).first()``.
        if zone is None or zone.user_id != user.id:
            raise HTTPException(status_code=404, detail="Zone not found.")
        elevation_m = float(zone.elevation_m or 0.0)
        zone_lat = getattr(zone, "humidity_weather_latitude", None)
        zone_lon = getattr(zone, "humidity_weather_longitude", None)

        row = session.get(CustomUserCustomuser, user.id)
        latitude = getattr(row, "latitude", None)
        longitude = getattr(row, "longitude", None)

    # Django uses timezone.now().date() (USE_TZ=True → UTC).
    start = datetime.datetime.now(datetime.timezone.utc).date()
    daily = get_daily_forecast(
        start=start, days=days, latitude=latitude, longitude=longitude
    )
    forecast = et0_forecast(
        daily, latitude=latitude, longitude=longitude, elevation_m=elevation_m
    )

    # Real reference curve: Open-Meteo's own published FAO-56 ET₀, keyed by ISO
    # date. Coordinates: the farmer's picked location (lat/lon params) wins, then
    # the account lat/lon, then the zone's stored weather coordinates — so the
    # curve populates even for the common case of a null-coordinate account.
    # Best-effort — an empty map leaves ``et0_openmeteo_mm`` null on every day.
    om_lat, om_lon = _pick_coords(
        (lat, lon), (latitude, longitude), (zone_lat, zone_lon)
    )
    reference = fetch_openmeteo_et0(
        start=start, days=days, latitude=om_lat, longitude=om_lon
    )
    for day in forecast:
        day["et0_openmeteo_mm"] = reference.get(day["date"])

    return {
        "zone_id": zone_id,
        "provider": active_provider(),
        "reference_provider": "open-meteo",
        "days": forecast,
    }


@router.get(
    "/weather/et0-series",
    summary="Open-Meteo daily reference ET₀ over a date range for one zone",
)
def et0_series(
    zone: int,
    start_date: str | None = None,
    end_date: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    user: AuthedUser = Depends(get_current_user),
):
    """Daily Open-Meteo FAO-56 ET₀ (mm/day) for the caller's ``zone`` across the
    requested range, shaped like a sensor-reading series so the ET₀ comparison
    chart can plot it as a third line next to the sensor + calculated series.

    Owner-scoped (same 404 as et-forecast). Best-effort: returns ``[]`` when
    Open-Meteo is unreachable / out of its window, or the user has no lat/lon.
    Each point is stamped at noon of its day so it sits mid-day on the chart.
    """
    with session_scope() as session:
        z = session.get(AnalyticsZone, zone)
        # Owner-scoped: unknown vs not-owned are indistinguishable (same 404).
        if z is None or z.user_id != user.id:
            raise HTTPException(status_code=404, detail="Zone not found.")
        zone_lat = getattr(z, "humidity_weather_latitude", None)
        zone_lon = getattr(z, "humidity_weather_longitude", None)
        row = session.get(CustomUserCustomuser, user.id)
        latitude = getattr(row, "latitude", None)
        longitude = getattr(row, "longitude", None)

    today = datetime.datetime.now(datetime.timezone.utc).date()
    start = _parse_iso_date(start_date) or today
    end = _parse_iso_date(end_date) or (start + datetime.timedelta(days=6))
    if end < start:
        end = start
    days = min(_MAX_SERIES_DAYS, (end - start).days + 1)

    # Coordinates: farmer's picked location wins, then account, then zone.
    om_lat, om_lon = _pick_coords(
        (lat, lon), (latitude, longitude), (zone_lat, zone_lon)
    )
    reference = fetch_openmeteo_et0(
        start=start, days=days, latitude=om_lat, longitude=om_lon
    )
    return [
        {
            "timestamp": f"{iso}T12:00:00",
            "value": value,
            "default_unit": "mm/day",
            "available_units": ["mm/day"],
        }
        for iso, value in sorted(reference.items())
    ]
