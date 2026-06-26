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
    """Most recent value the alert applies to. Scopes to the alert's farm
    zone, or — for a notification-zone alert — the source zone of its matching
    sensor assignment; falls back to the owner's data when no zone is
    configured. ``LatestReading(None, None)`` when nothing is available so
    callers don't need to handle ``DoesNotExist``.
    """
    if not alert.sensor_key or alert.sensor_key not in SENSOR_KEY_REGISTRY:
        return LatestReading(None, None)

    model = get_sensor_model(alert.sensor_key)
    qs = model.objects.all()
    if getattr(alert, "notification_zone_id", None):
        # Notification-zone alert (#57): scope to the source zone of the
        # matching sensor assignment (source_zone NULL = the owner's data,
        # mirroring agri-core's effective_zone_id_for_alert).
        from analytics.models import NotificationZoneSensor

        assignment = (
            NotificationZoneSensor.objects.filter(
                notification_zone_id=alert.notification_zone_id,
                sensor_key=alert.sensor_key,
            )
            .order_by("id")
            .first()
        )
        if assignment is not None and assignment.source_zone_id:
            qs = qs.filter(zone_id=assignment.source_zone_id)
        elif alert.user_id:
            qs = qs.filter(user_id=alert.user_id)
    elif alert.zone_id:
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
    """Thin adapter: delegate to agri-core's DB-backed fan-out.

    Opens an ``agri.core.database`` session (commit=True so the core handler's
    ``last_triggered_at`` stamps persist) and lets agri-core fetch + evaluate.
    """
    from agri.core.alerts import recent_triggers_for_user as _core_recent_triggers
    from agri.core.database import session_scope

    with session_scope(commit=True) as session:
        return _core_recent_triggers(
            session,
            user.id,
            sensor_key=sensor_key,
            zone_id=zone_id,
            now=timezone.now(),
        )


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
    alert reflects the data the database now contains. Returns the count of
    triggered alerts that won the grace claim and were dispatched on at least
    one channel (email/WhatsApp/SMS) — useful for tests.
    """
    if value is None:
        return 0
    if sensor_key not in SENSOR_KEY_REGISTRY:
        return 0

    from django.db.models import Q

    from analytics.models import Alert, NotificationZoneSensor

    # Custom notification zones (agrilogy-front #57): an alert bound to a
    # notification zone fires only when one of that zone's sensor assignments
    # feeds this (sensor_key, source_zone) — source_zone NULL = any zone.
    nz_sensor_q = Q(sensor_key=sensor_key)
    if zone is not None:
        nz_sensor_q &= Q(source_zone=zone) | Q(source_zone__isnull=True)
    else:
        nz_sensor_q &= Q(source_zone__isnull=True)
    matching_nz_ids = list(
        NotificationZoneSensor.objects.filter(nz_sensor_q).values_list(
            "notification_zone_id", flat=True
        )
    )

    # True user-wide alerts: no farm zone AND no notification zone.
    match = Q(zone__isnull=True, notification_zone__isnull=True)
    if zone is not None:
        match |= Q(zone=zone, notification_zone__isnull=True)
    if matching_nz_ids:
        match |= Q(notification_zone_id__in=matching_nz_ids)

    alerts_qs = Alert.objects.filter(
        user=user,
        sensor_key=sensor_key,
        is_active=True,
    ).filter(match)

    now_ts = timezone.now()
    default_grace = grace_period_seconds_for(sensor_key)
    enqueued = 0
    email_alert_ids: list[int] = []

    for alert in alerts_qs:
        if not evaluate_alert(alert, value):
            continue

        # An alert with no delivery channel enabled would win the grace claim
        # and stamp last_emailed_at / last_triggered_at without sending
        # anything, burning the cadence slot for nothing. Skip it before the
        # claim so a later re-enabled channel isn't left mid-grace.
        if not (
            getattr(alert, "notify_email", True)
            or getattr(alert, "notify_whatsapp", False)
            or getattr(alert, "notify_sms", False)
        ):
            continue

        from django.db.models import Q

        # Per-alert grace override (#37): a non-null grace_override_seconds beats
        # the global per-sensor default so a user can ask for a tighter cadence
        # on one critical alert. NULL falls back to the sensor's global grace.
        override = getattr(alert, "grace_override_seconds", None)
        grace_seconds = override if override is not None else default_grace
        cutoff = now_ts - timedelta(seconds=grace_seconds)

        won = (
            Alert.objects.filter(pk=alert.pk)
            .filter(Q(last_emailed_at__isnull=True) | Q(last_emailed_at__lt=cutoff))
            .update(last_emailed_at=now_ts, last_triggered_at=now_ts)
        )
        if not won:
            continue

        from agriapi.tasks import send_alert_sms, send_alert_whatsapp

        # Fan out to the channels this alert opted into. Legacy alerts default
        # notify_email=True / notify_whatsapp=False / notify_sms=False, so
        # behaviour is unchanged unless the user explicitly enables a channel.
        # Email is collected and sent once below (digest when several alerts on
        # the same reading fire); WhatsApp/SMS stay per-alert.
        kwargs = dict(
            alert_id=alert.pk,
            value=float(value),
            timestamp_iso=timestamp.isoformat(),
        )
        if getattr(alert, "notify_email", True):
            email_alert_ids.append(alert.pk)
        if getattr(alert, "notify_whatsapp", False):
            send_alert_whatsapp.delay(**kwargs)
        if getattr(alert, "notify_sms", False):
            send_alert_sms.delay(**kwargs)
        enqueued += 1

    # Aggregation digest (#37): when more than one alert on this same reading
    # opted into email, send ONE combined email instead of N. A single alert
    # keeps the original per-alert email unchanged. The atomic grace claim above
    # already ran per alert, so no dedup regression.
    if email_alert_ids:
        from agriapi.tasks import send_alert_digest_email, send_alert_email

        value_f = float(value)
        ts_iso = timestamp.isoformat()
        if len(email_alert_ids) == 1:
            send_alert_email.delay(
                alert_id=email_alert_ids[0], value=value_f, timestamp_iso=ts_iso
            )
        else:
            send_alert_digest_email.delay(
                alert_ids=email_alert_ids, value=value_f, timestamp_iso=ts_iso
            )

    return enqueued


# ----- 6. Suggestion (Django-side wrapper around the pure payload assembly)


def suggest_alert(
    user,
    *,
    sensor_key: str,
    zone_id: int | None = None,
    sample_size: int = 50,
    strategy: str = "mean",
) -> dict[str, Any] | None:
    """Build a sensible default payload for the create-alert form.

    Fetches the most-recent ``sample_size`` readings for ``sensor_key``
    (scoped to ``user`` + optional ``zone_id``) and hands them off to
    ``agri.core.alerts.suggested_alert_payload``. ``strategy`` selects how
    the threshold is derived (mean / percentile / sd). Returns ``None`` when
    the sensor key is unknown.
    """
    from agri.core.alerts import suggest_alert_for
    from agri.core.database import session_scope

    with session_scope() as session:
        return suggest_alert_for(
            session,
            user.id,
            sensor_key,
            zone_id=zone_id,
            limit=sample_size,
            strategy=strategy,
        )


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
