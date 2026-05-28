"""Plug-and-play alert engine — Django adapter around ``agri.core.alerts``.

The pure pieces (sensor-key registry, threshold predicate, `AlertSpec`
DTO, `suggested_alert_payload`) live in ``agri.core.alerts``. This
module re-exports them and keeps the Django-coupled bits:

* ``get_sensor_model`` — resolves a sensor_key's registry-string to a
  live Django model class.
* ``latest_value_for(alert)`` — most-recent ORM row.
* ``recent_triggers_for_user(user, ...)`` — fan-out across the user's
  active alerts; annotates each with its latest value.
* ``dispatch_alerts_for_reading(...)`` — push-on-ingest dispatch with
  the per-key grace-period gate and Celery email enqueue.
* ``grace_period_seconds_for(sensor_key)`` — reads Django settings.
* ``suggest_alert(user, ...)`` — fetches recent values, calls
  ``suggested_alert_payload``.

``evaluate_alert(alert, value)`` is a 1-line wrapper that packs an
``AlertSpec`` from the Django row before delegating.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from django.utils import timezone

# Re-exports from agri-core. Per memory `project_agri_core_architecture`.
from agri.core.alerts import (
    EQUAL_TO,
    EQUALITY_TOLERANCE,
    GREATER_THAN,
    LESS_THAN,
    SENSOR_KEY_REGISTRY,
    AlertSpec,
    LatestReading,
    evaluate,
    suggested_alert_payload,
)
from agri.core.alerts import evaluate_alert as _core_evaluate_alert


# ----- 1. Sensor model resolution (Django-coupled) ------------------------


def get_sensor_model(sensor_key: str):
    """Resolve a sensor_key to its Django model class. KeyError when unknown."""
    from analytics import models as analytics_models

    spec = SENSOR_KEY_REGISTRY[sensor_key]
    return getattr(analytics_models, spec["model"])


# ----- 2. Bind agri-core's evaluator to a Django Alert row -----------------


def evaluate_alert(alert, value: float | None) -> bool:
    """True when ``value`` violates the alert's threshold."""
    return _core_evaluate_alert(
        AlertSpec(condition=alert.condition, threshold=float(alert.condition_nbr)),
        value,
    )


# ----- 3. Latest reading helper (Django-coupled) --------------------------


def latest_value_for(alert) -> LatestReading:
    """Most recent value the alert applies to. Falls back to the alert's
    owner when no zone is configured. ``LatestReading(None, None)`` when
    nothing is available so callers don't need to handle ``DoesNotExist``.
    """
    if not alert.sensor_key or alert.sensor_key not in SENSOR_KEY_REGISTRY:
        return LatestReading(None, None)

    model = get_sensor_model(alert.sensor_key)
    qs = model.objects.all()
    if alert.zone_id:
        qs = qs.filter(zone_id=alert.zone_id)
    elif alert.user_id:
        qs = qs.filter(user_id=alert.user_id)
    row = qs.order_by("-timestamp").first()
    if row is None:
        return LatestReading(None, None)
    return LatestReading(value=row.value, timestamp=row.timestamp)


# ----- 4. Fan-out --------------------------------------------------------


def recent_triggers_for_user(
    user, *, sensor_key: str | None = None, zone_id: int | None = None
) -> list[dict[str, Any]]:
    """Return every active alert for ``user`` (optionally filtered to one
    sensor/zone) annotated with its latest value, whether it's currently
    triggered, and the canonical threshold for chart overlays.
    """
    from analytics.models import Alert

    qs = Alert.objects.filter(user=user, is_active=True)
    if sensor_key:
        qs = qs.filter(sensor_key=sensor_key)
    if zone_id:
        qs = qs.filter(zone_id=zone_id)

    out: list[dict[str, Any]] = []
    now = timezone.now()
    for alert in qs.order_by("id"):
        latest = latest_value_for(alert)
        triggered = evaluate_alert(alert, latest.value)
        if triggered and not alert.last_triggered_at:
            alert.last_triggered_at = now
            alert.save(update_fields=["last_triggered_at"])
        out.append(
            {
                "id": alert.id,
                "name": alert.name,
                "sensor_key": alert.sensor_key,
                "zone_id": alert.zone_id,
                "condition": alert.condition,
                "threshold": float(alert.condition_nbr),
                "unit": SENSOR_KEY_REGISTRY.get(alert.sensor_key, {}).get("unit"),
                "label": SENSOR_KEY_REGISTRY.get(alert.sensor_key, {}).get("label"),
                "is_active": alert.is_active,
                "latest_value": latest.value,
                "latest_timestamp": latest.timestamp.isoformat()
                if latest.timestamp
                else None,
                "is_triggered": triggered,
                "last_triggered_at": (
                    alert.last_triggered_at.isoformat()
                    if alert.last_triggered_at
                    else None
                ),
            }
        )
    return out


def assert_keys_resolve(keys: Iterable[str]) -> None:
    """Test helper: every key in the registry must point at a live model."""
    for key in keys:
        get_sensor_model(key)


# ----- 5. Push-on-ingest dispatch ----------------------------------------


def grace_period_seconds_for(sensor_key: str) -> int:
    """Per-sensor_key cool-down between alert emails, in seconds.

    Reads ``settings.ALERT_GRACE_PERIODS`` first, then falls back to
    ``settings.DEFAULT_ALERT_GRACE_PERIOD`` (and finally 1800 s when even
    that is unset, so older settings.py files keep booting).
    """
    from django.conf import settings

    table = getattr(settings, "ALERT_GRACE_PERIODS", {}) or {}
    default = getattr(settings, "DEFAULT_ALERT_GRACE_PERIOD", 1800)
    return int(table.get(sensor_key, default))


def dispatch_alerts_for_reading(
    *,
    sensor_key: str,
    zone,
    user,
    value: float | None,
    timestamp: datetime,
) -> int:
    """Evaluate every active alert that matches ``(user, sensor_key, zone)``
    against ``value`` and enqueue one alert email per alert that:
      • is currently triggered, AND
      • whose ``last_emailed_at`` is older than this sensor's grace period
        (or has never been emailed at all).

    The grace gate is applied via a CONDITIONAL UPDATE on ``last_emailed_at``
    so a burst of readings arriving simultaneously cannot dispatch the same
    email twice — only the row whose UPDATE actually flipped the timestamp
    enqueues a task.

    Called from the ingest path AFTER the sensor row is created so the
    alert reflects the data the database now contains. Returns the number
    of emails enqueued (useful for tests).
    """
    if value is None:
        return 0
    if sensor_key not in SENSOR_KEY_REGISTRY:
        return 0

    from analytics.models import Alert

    alerts_qs = Alert.objects.filter(
        user=user,
        sensor_key=sensor_key,
        is_active=True,
    )
    if zone is not None:
        from django.db.models import Q

        alerts_qs = alerts_qs.filter(Q(zone=zone) | Q(zone__isnull=True))
    else:
        alerts_qs = alerts_qs.filter(zone__isnull=True)

    now_ts = timezone.now()
    grace_seconds = grace_period_seconds_for(sensor_key)
    cutoff = now_ts - timedelta(seconds=grace_seconds)
    enqueued = 0

    for alert in alerts_qs:
        if not evaluate_alert(alert, value):
            continue

        from django.db.models import Q

        won = Alert.objects.filter(pk=alert.pk).filter(
            Q(last_emailed_at__isnull=True) | Q(last_emailed_at__lt=cutoff)
        ).update(last_emailed_at=now_ts, last_triggered_at=now_ts)
        if not won:
            continue

        from agriBack.tasks import send_alert_email

        send_alert_email.delay(
            alert_id=alert.pk,
            value=float(value),
            timestamp_iso=timestamp.isoformat(),
        )
        enqueued += 1

    return enqueued


# ----- 6. Suggestion (Django-side wrapper around the pure payload assembly)


def suggest_alert(
    user, *, sensor_key: str, zone_id: int | None = None, sample_size: int = 50
) -> dict[str, Any] | None:
    """Build a sensible default payload for the create-alert form.

    Fetches the most-recent ``sample_size`` readings for ``sensor_key``
    (scoped to ``user`` + optional ``zone_id``) and hands them off to
    ``agri.core.alerts.suggested_alert_payload``. Returns ``None`` when
    the sensor key is unknown.
    """
    if sensor_key not in SENSOR_KEY_REGISTRY:
        return None

    model = get_sensor_model(sensor_key)
    qs = model.objects.all()
    if user is not None:
        qs = qs.filter(user=user)
    if zone_id:
        qs = qs.filter(zone_id=zone_id)

    recent = list(
        qs.order_by("-timestamp").values_list("value", flat=True)[:sample_size]
    )
    recent_values = [float(v) for v in recent if v is not None]
    return suggested_alert_payload(sensor_key, recent_values)


# Backward-compat re-exports for callers that referenced these as module-level
# names from this module (they now actually live in agri.core.alerts).
__all__ = [
    "EQUAL_TO",
    "EQUALITY_TOLERANCE",
    "GREATER_THAN",
    "LESS_THAN",
    "SENSOR_KEY_REGISTRY",
    "AlertSpec",
    "LatestReading",
    "assert_keys_resolve",
    "dispatch_alerts_for_reading",
    "evaluate",
    "evaluate_alert",
    "get_sensor_model",
    "grace_period_seconds_for",
    "latest_value_for",
    "recent_triggers_for_user",
    "suggest_alert",
    "suggested_alert_payload",
]
