"""Ops beat jobs: ingest staleness alerting (agri-api #429).

The LoRa feed once died for nine days without anyone noticing, because
nothing alerted on *absence* of data. ``check_ingest_staleness`` runs on beat,
reads the newest timestamp of each ingest feed and emails the ops recipients
when a feed has been silent for longer than ``INGEST_STALE_HOURS``.

- One email per feed per ``INGEST_STALE_RENOTIFY_HOURS`` (Redis ``SET NX EX``
  slot, so concurrent beats or retries can't double-send). No schema change.
- When a feed that was alerted on starts flowing again, the slot is released
  and a one-off "recovered" email goes out, so the next outage alerts at once.
- A feed whose table is missing or has never held a row is skipped: there is
  nothing to be stale *from*, and an empty dev database must not page anyone.

Transport-agnostic on purpose: a dead gateway produces no logs, so only the
data itself can tell.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from agri.core.database import session_scope
from sqlalchemy import text

from fastapp.settings import get_settings
from fastapp.tasks_comms import _send_email

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestFeed:
    key: str
    label: str
    table: str
    column: str


# Fixed identifiers (never user input) — safe to interpolate into the SQL.
INGEST_FEEDS: tuple[IngestFeed, ...] = (
    IngestFeed("lora", "LoRaWAN uplinks (ChirpStack)", "lora_uplink", "received_at"),
    IngestFeed(
        "bivocom",
        "Bivocom / router weather readings",
        "analytics_temperatureweather",
        "timestamp",
    ),
)

_SLOT_PREFIX = "ingest-stale:"
_redis_client = None


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(ts: datetime.datetime) -> datetime.datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)


def find_stale_feeds(
    latest: dict[str, datetime.datetime | None],
    *,
    now: datetime.datetime,
    threshold_hours: float,
) -> dict[str, float]:
    """Pure: ``{feed_key: hours_silent}`` for every feed whose newest row is
    older than ``threshold_hours``. ``None`` (no table / no rows) is skipped."""
    out: dict[str, float] = {}
    for key, ts in latest.items():
        if ts is None:
            continue
        hours = (now - _aware(ts)).total_seconds() / 3600
        if hours > threshold_hours:
            out[key] = round(hours, 1)
    return out


def _latest_timestamps(session) -> dict[str, datetime.datetime | None]:
    latest: dict[str, datetime.datetime | None] = {}
    for feed in INGEST_FEEDS:
        exists = session.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": feed.table}
        ).scalar()
        if not exists:
            latest[feed.key] = None
            continue
        latest[feed.key] = session.execute(
            text(f'SELECT max("{feed.column}") FROM "{feed.table}"')
        ).scalar()
    return latest


def _redis():
    global _redis_client
    import redis

    if _redis_client is None:
        _redis_client = redis.from_url(get_settings().celery_broker_url)
    return _redis_client


def claim_alert_slot(feed_key: str, ttl_seconds: int) -> bool:
    """True when we win the once-per-window slot for ``feed_key``. A Redis
    error fails closed (no email) rather than spamming. Monkeypatched in tests."""
    try:
        return bool(_redis().set(_SLOT_PREFIX + feed_key, 1, nx=True, ex=ttl_seconds))
    except Exception:  # pragma: no cover - Redis unreachable → don't spam
        logger.warning("ingest_staleness: redis unavailable, skipping %s", feed_key)
        return False


def release_alert_slot(feed_key: str) -> bool:
    """Drop ``feed_key``'s slot. True when a slot existed, i.e. the feed had
    been alerted on and has now recovered. Monkeypatched in tests."""
    try:
        return bool(_redis().delete(_SLOT_PREFIX + feed_key))
    except Exception:  # pragma: no cover
        return False


def _fmt(ts: datetime.datetime) -> str:
    return _aware(ts).strftime("%Y-%m-%d %H:%M UTC")


def check_ingest_staleness() -> dict:
    """Beat job: email ops when an ingest feed has gone quiet (see module doc)."""
    s = get_settings()
    now = _utcnow()
    with session_scope() as session:
        latest = _latest_timestamps(session)
    stale = find_stale_feeds(latest, now=now, threshold_hours=s.ingest_stale_hours)
    recipients = s.ops_alert_emails_list
    ttl = int(s.ingest_stale_renotify_hours * 3600)
    feeds = {f.key: f for f in INGEST_FEEDS}
    alerted: list[str] = []
    recovered: list[str] = []

    for key, hours in stale.items():
        if not recipients or not claim_alert_slot(key, ttl):
            continue
        feed = feeds[key]
        subject = f"[Agrogo ops] No {feed.label} for {hours:g} h"
        body = (
            f"The {feed.label} feed has received no data since "
            f"{_fmt(latest[key])} ({hours:g} hours, threshold "
            f"{s.ingest_stale_hours:g} h).\n\n"
            f"Source: {feed.table}.{feed.column}\n"
            "Check the gateway / routers and the bridge + MQTT containers.\n\n"
            f"You will be reminded every {s.ingest_stale_renotify_hours} h "
            "while it stays silent, and told when data resumes."
        )
        sent = sum(_send_email(to=r, subject=subject, body=body) for r in recipients)
        logger.warning(
            "ingest_staleness: %s silent %.1f h, emailed %d", key, hours, sent
        )
        alerted.append(key)

    for key, ts in latest.items():
        if ts is None or key in stale or not release_alert_slot(key):
            continue
        feed = feeds[key]
        subject = f"[Agrogo ops] {feed.label} data resumed"
        body = f"The {feed.label} feed is receiving data again (latest {_fmt(ts)})."
        for r in recipients:
            _send_email(to=r, subject=subject, body=body)
        logger.info("ingest_staleness: %s recovered", key)
        recovered.append(key)

    return {
        "stale": stale,
        "alerted": alerted,
        "recovered": recovered,
        "checked": sorted(k for k, v in latest.items() if v is not None),
    }
