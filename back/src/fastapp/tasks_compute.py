"""fastapp compute/scan task bodies (F8b) — the periodic beat jobs.

Django-free ports of the compute + scan Celery tasks in ``agriapi/tasks.py``.
Plain functions here; the native Celery app (F10) wraps them under the SAME
names (``agriapi.tasks.<name>``) with the static beat schedule. Additive until
then — the Django worker keeps running everything.

All physics lives in agri-core (``agri.core.agronomy``); this module only reads
zones and upserts results via the agri-core SQLAlchemy session.
"""

from __future__ import annotations

import datetime
import logging
import os

from agri.core.agronomy import compute_et0_for_zone
from agri.core.database import session_scope
from agri.db.analytics import (
    AnalyticsEt0calculated,
    AnalyticsVpdweather,
    AnalyticsZone,
)
from agri.core.notifications import compose_notification_for_user
from agri.db.users import CustomUserCustomuser
from sqlalchemy import func, or_, select, update

from fastapp.history import record_notification_decision
from fastapp.settings import get_settings
from fastapp.tasks_comms import _record_delivery, _send_email

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _upsert_value(session, model, *, zone_id: int, timestamp, user_id, value) -> None:
    """update_or_create by (zone, timestamp) — keep exactly one row per bucket."""
    row = session.execute(
        select(model).where(model.zone_id == zone_id, model.timestamp == timestamp)
    ).scalar_one_or_none()
    if row is not None:
        row.value = value
        row.user_id = user_id
    else:
        session.add(
            model(zone_id=zone_id, timestamp=timestamp, user_id=user_id, value=value)
        )


def compute_et0_vpd_hourly() -> dict:
    """For each zone, ask agri-core for one hour of ET0 + VPD and persist it.

    Idempotent per (zone, timestamp): ``update_or_create`` keeps exactly one row
    per hour bucket so retries / double-fires don't accumulate duplicates.
    """
    with session_scope(commit=True) as session:
        zone_ids = list(session.scalars(select(AnalyticsZone.id)).all())
        written = 0
        for zid in zone_ids:
            result = compute_et0_for_zone(session, zid)
            if result is None:
                continue
            _upsert_value(
                session,
                AnalyticsEt0calculated,
                zone_id=zid,
                timestamp=result.timestamp,
                user_id=result.user_id,
                value=result.et0_mm_per_h,
            )
            _upsert_value(
                session,
                AnalyticsVpdweather,
                zone_id=zid,
                timestamp=result.timestamp,
                user_id=result.user_id,
                value=result.vpd_kpa,
            )
            written += 1
    return {
        "zones_processed": len(zone_ids),
        "et0_rows": written,
        "vpd_rows": written,
    }


# ---------------------------------------------------------------------------
# flag_idle_zones — email a zone owner when its sensors go silent
# ---------------------------------------------------------------------------
_redis_client = None


def claim_reflag_slot(zone_id: int, ttl_seconds: int) -> bool:
    """Throttle re-flagging to once per reflag window. Redis ``SET NX EX`` —
    True when we claim the slot (equivalent to Django's ``cache.add``). Broker
    Redis is up whenever the task runs, so a Redis error fails closed (skip).
    Monkeypatchable in tests (no Redis needed there)."""
    global _redis_client
    try:
        import redis

        if _redis_client is None:
            _redis_client = redis.from_url(get_settings().celery_broker_url)
        return bool(
            _redis_client.set(f"zone-idle-flag:{zone_id}", 1, nx=True, ex=ttl_seconds)
        )
    except Exception:  # pragma: no cover - Redis unreachable → don't spam
        return False


def _idle_sensor_models() -> list[type]:
    """Distinct agri.db sensor models to scan for a zone's latest reading —
    the same set Django derives from ``SENSOR_KEY_REGISTRY`` model names."""
    import agri.db.analytics as analytics

    from agri.core.alerts import SENSOR_KEY_REGISTRY

    out: list[type] = []
    for name in {spec["model"] for spec in SENSOR_KEY_REGISTRY.values()}:
        cls = getattr(analytics, "Analytics" + name.capitalize(), None)
        if cls is not None:
            out.append(cls)
    return out


def flag_idle_zones() -> dict:
    """Email a zone's owner when no sensor has reported in longer than
    ``ZONE_IDLE_THRESHOLD_HOURS``. Skips zones that never reported (empty/demo)
    and fresh zones; a reflag slot throttles to once per window. Django-free
    port of ``agriapi.tasks.flag_idle_zones``."""
    threshold_hours = int(os.getenv("ZONE_IDLE_THRESHOLD_HOURS", "24"))
    reflag_hours = int(os.getenv("ZONE_IDLE_REFLAG_HOURS", str(threshold_hours)))
    now = _utcnow()
    cutoff = now - datetime.timedelta(hours=threshold_hours)
    models = _idle_sensor_models()

    flagged = 0
    with session_scope(commit=True) as session:
        zones = session.scalars(select(AnalyticsZone).order_by(AnalyticsZone.id)).all()
        for zone in zones:
            latest = None
            for model in models:
                ts = session.scalar(
                    select(func.max(model.timestamp)).where(model.zone_id == zone.id)
                )
                if ts and (latest is None or ts > latest):
                    latest = ts
            # Skip never-reported (empty/demo) and still-fresh zones.
            if latest is None or latest >= cutoff:
                continue
            # Claim the reflag slot BEFORE the recipient check (mirrors Django):
            # a no-recipient zone still consumes its slot for the window.
            if not claim_reflag_slot(zone.id, reflag_hours * 3600):
                continue
            user = (
                session.get(CustomUserCustomuser, zone.user_id)
                if zone.user_id
                else None
            )
            recipient = (getattr(user, "email", "") or "").strip() if user else ""
            if not recipient:
                continue
            last_seen = latest.isoformat()
            subject = f"Aucune donnée récente — {zone.name}"
            body = (
                f"Bonjour {getattr(user, 'firstname', '') or user.username},\n\n"
                f"La zone « {zone.name} » n'a reçu aucune nouvelle mesure depuis "
                f"plus de {threshold_hours} h (dernière donnée : {last_seen}).\n\n"
                f"Vérifiez vos capteurs et la connectivité du terrain.\n"
            )
            ok = _send_email(to=recipient, subject=subject, body=body)
            if not ok:
                _record_delivery(
                    session,
                    channel="email",
                    kind="liveness",
                    recipient=recipient,
                    user_id=user.id,
                    status="failed",
                    error="send_error",
                )
                continue
            _record_delivery(
                session,
                channel="email",
                kind="liveness",
                recipient=recipient,
                user_id=user.id,
                status="sent",
            )
            flagged += 1

    logger.info("flag_idle_zones: flagged=%d zones", flagged)
    return {"flagged": flagged}


# ---------------------------------------------------------------------------
# send_periodic_notifications — the cadence-gated field-status digest
# ---------------------------------------------------------------------------
def _claim_notification_slot(session, user_id: int, notify_every: int, moment) -> bool:
    """Atomically claim the user's periodic slot: a single conditional UPDATE
    that stamps ``last_notified = moment`` only when the cadence window elapsed.
    True when THIS call won the slot (rowcount == 1). Advancing the clock
    up-front means a provider failure won't re-attempt every tick (#180)."""
    cutoff = moment - datetime.timedelta(minutes=notify_every)
    result = session.execute(
        update(CustomUserCustomuser)
        .where(
            CustomUserCustomuser.id == user_id,
            or_(
                CustomUserCustomuser.last_notified.is_(None),
                CustomUserCustomuser.last_notified < cutoff,
            ),
        )
        .values(last_notified=moment)
    )
    return result.rowcount == 1


def send_periodic_notifications() -> dict:
    """For every active user with an email, dispatch a personalised field-status
    notification when their cadence (notify_every minutes) has elapsed since
    last_notified. Per-user failures don't abort the batch. Django-free port of
    ``agriapi.tasks.send_periodic_notifications``."""
    with session_scope() as session:
        user_ids = list(
            session.scalars(
                select(CustomUserCustomuser.id)
                .where(CustomUserCustomuser.is_active.is_(True))
                .order_by(CustomUserCustomuser.id)
            ).all()
        )

    sent = skipped = failed = 0
    for uid in user_ids:
        moment = _utcnow()
        with session_scope() as session:
            user = session.get(CustomUserCustomuser, uid)
            if user is None:
                continue
            recipient = (getattr(user, "email", "") or "").strip()
            notify_every = getattr(user, "notify_every", 240) or 240
        if not recipient:
            skipped += 1
            continue
        # Claim in its own committed txn so the row lock is released before the
        # (slow, network) send — mirrors the Django autocommit UPDATE.
        with session_scope(commit=True) as session:
            claimed = _claim_notification_slot(session, uid, notify_every, moment)
        if not claimed:
            skipped += 1
            continue
        status, error = "sent", ""
        try:
            with session_scope() as session:
                body = compose_notification_for_user(session, uid, now=moment)
            ok = _send_email(
                to=recipient,
                subject="Mise à jour de votre terrain agricole",
                body=body or "",
            )
            if not ok:
                raise RuntimeError("send_error")
            sent += 1
        except Exception as exc:
            failed += 1
            status, error = "failed", str(exc)
            logger.exception("Failed to send periodic notification to %s", recipient)
        with session_scope(commit=True) as session:
            _record_delivery(
                session,
                channel="email",
                kind="periodic",
                recipient=recipient,
                user_id=uid,
                status=status,
                error=error,
            )
            # RPT-1 (#441): the recommendation this email carried is history.
            # Best-effort and probe-gated — never a reason for the digest to
            # fail (it has already been sent by this point anyway).
            if status == "sent":
                record_notification_decision(
                    session, uid, source="periodic", now=moment
                )

    logger.info(
        "Periodic notifications: sent=%d, skipped=%d, failed=%d", sent, skipped, failed
    )
    return {"sent": sent, "skipped": skipped, "failed": failed}
