"""7-day reference-ET₀ forecast endpoint (agrilogy-front #18).

    GET /weather/et-forecast?zone_id=<id>&days=<n>

Resolves the caller's zone, pulls daily weather from the forecast provider
(mock by default — see ``forecast_provider``), and runs the pure agri-core
ET₀-forecast compute. Read-only; owner-scoped.
"""

from __future__ import annotations

from datetime import date  # noqa: F401  (kept for type clarity)

from django.utils import timezone
from ninja import Router
from ninja.responses import Response

from agri.core.et_forecast import et0_forecast
from agriapi.api.auth import JwtAuth
from apps.sensors.forecast_provider import active_provider, get_daily_forecast

router = Router()

_MAX_DAYS = 14


@router.get(
    "/et-forecast",
    auth=JwtAuth(),
    summary="Daily reference-ET0 forecast for one of the caller's zones",
)
def et_forecast(request, zone_id: int, days: int = 7):
    from apps.irrigation.models import Zone

    days = max(1, min(_MAX_DAYS, days))

    zone = Zone.objects.filter(id=zone_id, user=request.auth).first()
    if zone is None:
        return Response({"detail": "Zone not found."}, status=404)

    user = request.auth
    latitude = getattr(user, "latitude", None)
    longitude = getattr(user, "longitude", None)
    elevation_m = float(getattr(zone, "elevation_m", 0.0) or 0.0)

    start = timezone.now().date()
    daily = get_daily_forecast(
        start=start, days=days, latitude=latitude, longitude=longitude
    )
    forecast = et0_forecast(
        daily, latitude=latitude, longitude=longitude, elevation_m=elevation_m
    )

    return {
        "zone_id": zone.id,
        "provider": active_provider(),
        "days": forecast,
    }
