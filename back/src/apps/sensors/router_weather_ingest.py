"""Multi-sensor weather ingest router (django-ninja).

Migrated from ``analytics.views.WeatherIngestAPIView``:

  * POST /api/sensors/weather/ingest/

Accepts the Node bridge's flat JSON object whose keys overlap
``SENSOR_KEY_REGISTRY``. Each registry key with a non-None value is
persisted to the matching model and pushed through
``dispatch_alerts_for_reading`` so alert emails can fire. NPK is
intentionally excluded — its model has three value fields and needs
per-key routing (tracked separately).

Auth: ``auth=None`` because the bridge POSTs to this endpoint without
JWT today (it identifies the user via the ``client`` field in the
payload).
"""

from __future__ import annotations

import datetime as _dt
import logging

from django.utils import timezone
from ninja import Router, Schema
from ninja.responses import Response

from apps.alerts.engine import (
    SENSOR_KEY_REGISTRY,
    dispatch_alerts_for_reading,
    get_sensor_model,
)
from analytics.models import Zone
from apps.users.models import CustomUser

router = Router()
log = logging.getLogger(__name__)

_INGEST_SKIP_KEYS: set[str] = {"npk"}


@router.post(
    "/weather",
    auth=None,
    summary="Multi-sensor weather ingest webhook (POST /ingest/weather)",
)
def weather_ingest(request):
    """Multi-sensor ingest. Payload shape is opaque (the registry drives
    which keys we accept), so we read ``request.body`` directly rather
    than declaring a pydantic schema for every possible sensor key.
    """
    import json as _json

    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return Response({"error": "Expected a JSON object"}, status=400)

    if not isinstance(payload, dict):
        return Response({"error": "Expected a JSON object"}, status=400)

    metrics = {
        key: payload[key]
        for key in SENSOR_KEY_REGISTRY
        if key in payload and payload[key] is not None and key not in _INGEST_SKIP_KEYS
    }

    if not metrics:
        return {"inserted": 0, "detail": "all_metrics_none"}

    client = payload.get("client")
    if not client or not isinstance(client, str):
        return Response(
            {"error": "client is required when any metric is provided"},
            status=400,
        )
    client = client.strip()

    user = CustomUser.objects.filter(username=client).first()
    if not user:
        return Response(
            {"error": f"User not found for client '{client}'"},
            status=400,
        )

    zone = Zone.objects.filter(user=user).first()
    if not zone:
        return Response(
            {"error": f"No zone found for user '{user.username}'"},
            status=400,
        )

    user = zone.user
    now = timezone.now()

    inserted = 0
    for sensor_key, value in metrics.items():
        model_cls = get_sensor_model(sensor_key)
        model_cls.objects.create(
            user=user,
            zone=zone,
            value=value,
            timestamp=now,
        )
        inserted += 1
        # Alert dispatch must never abort the ingest loop: the sensor row
        # is already persisted, and a downstream alerts bug (schema drift,
        # broker outage, etc.) shouldn't drop the remaining keys in the
        # payload.
        try:
            dispatch_alerts_for_reading(
                sensor_key=sensor_key,
                zone=zone,
                user=user,
                value=value,
                timestamp=now,
            )
        except Exception:
            log.exception(
                "alert dispatch failed for sensor_key=%s user=%s zone=%s",
                sensor_key,
                getattr(user, "username", user),
                zone.id,
            )

    return Response({"inserted": inserted}, status=201)


# ---------------------------------------------------------------------------
# Single-sensor ingest (POST /ingest/sensor)
# ---------------------------------------------------------------------------
#
# A typed, one-reading-at-a-time device webhook — the clean contract for
# standalone sensors (water level / tank / basin probes, etc.) that don't
# speak the multi-metric weather-bridge payload. ``sensor_key`` must be a
# registry key with a single ``value`` model (NPK is excluded — it has three
# value fields). Like ``/ingest/weather`` it identifies the user via the
# ``client`` field and persists to that user's first zone, then pushes the
# reading through ``dispatch_alerts_for_reading`` so threshold alerts fire.


class SensorReadingIn(Schema):
    client: str
    sensor_key: str
    value: float
    timestamp: _dt.datetime | None = None


@router.post(
    "/sensor",
    auth=None,
    summary="Single-sensor ingest webhook (POST /ingest/sensor)",
)
def sensor_ingest(request, payload: SensorReadingIn):
    sensor_key = (payload.sensor_key or "").strip()
    if sensor_key not in SENSOR_KEY_REGISTRY or sensor_key in _INGEST_SKIP_KEYS:
        return Response(
            {"error": f"Unknown or unsupported sensor_key '{sensor_key}'"},
            status=400,
        )

    client = (payload.client or "").strip()
    if not client:
        return Response({"error": "client is required"}, status=400)

    user = CustomUser.objects.filter(username=client).first()
    if not user:
        return Response({"error": f"User not found for client '{client}'"}, status=400)

    zone = Zone.objects.filter(user=user).first()
    if not zone:
        return Response(
            {"error": f"No zone found for user '{user.username}'"}, status=400
        )

    user = zone.user
    ts = payload.timestamp or timezone.now()

    model_cls = get_sensor_model(sensor_key)
    model_cls.objects.create(user=user, zone=zone, value=payload.value, timestamp=ts)

    # Alert dispatch must never fail the ingest: the reading is already saved.
    try:
        dispatch_alerts_for_reading(
            sensor_key=sensor_key,
            zone=zone,
            user=user,
            value=payload.value,
            timestamp=ts,
        )
    except Exception:
        log.exception(
            "alert dispatch failed for sensor_key=%s user=%s zone=%s",
            sensor_key,
            getattr(user, "username", user),
            zone.id,
        )

    return Response({"inserted": 1, "sensor_key": sensor_key}, status=201)
