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
from apps.sensors.forecast_provider import active_provider, get_daily_forecast
from fastapp.auth import AuthedUser, get_current_user

router = APIRouter(tags=["weather"])

_MAX_DAYS = 14


@router.get(
    "/weather/et-forecast",
    summary="Daily reference-ET0 forecast for one of the caller's zones",
)
def et_forecast(
    zone_id: int,
    days: int = 7,
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

    return {
        "zone_id": zone_id,
        "provider": active_provider(),
        "days": forecast,
    }
