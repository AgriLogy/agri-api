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


def _recent_count(session, feed: IngestFeed, since: datetime.datetime) -> int:
    return int(
        session.execute(
            text(
                f'SELECT count(*) FROM "{feed.table}" WHERE "{feed.column}" >= :since'
            ),
            {"since": since},
        ).scalar()
        or 0
    )


def alert_active(feed_key: str) -> bool | None:
    """Whether an "ingest silent" alert is currently armed for the feed (its
    Redis slot is held). ``None`` when Redis can't be asked."""
    try:
        return bool(_redis().exists(_SLOT_PREFIX + feed_key))
    except Exception:  # pragma: no cover - Redis unreachable
        return None


def feed_status(session, *, now: datetime.datetime | None = None) -> dict:
    """Per-feed freshness for the admin monitoring page, using the same rule
    as the beat job: ``ok`` / ``stale`` (silent past ``INGEST_STALE_HOURS``) /
    ``no_data`` (table missing or empty), plus the rows received in the last
    hour so a feed coming back is visible at once."""
    s = get_settings()
    now = now or _utcnow()
    latest = _latest_timestamps(session)
    stale = find_stale_feeds(latest, now=now, threshold_hours=s.ingest_stale_hours)
    hour_ago = now - datetime.timedelta(hours=1)
    feeds = []
    for feed in INGEST_FEEDS:
        ts = latest[feed.key]
        if ts is None:
            status, hours, last_hour = "no_data", None, 0
        else:
            hours = round((now - _aware(ts)).total_seconds() / 3600, 1)
            status = "stale" if feed.key in stale else "ok"
            last_hour = _recent_count(session, feed, hour_ago)
        feeds.append(
            {
                "key": feed.key,
                "label": feed.label,
                "source": f"{feed.table}.{feed.column}",
                "status": status,
                "latest_at": _aware(ts).isoformat() if ts is not None else None,
                "hours_silent": hours,
                "last_hour_count": last_hour,
                "alert_active": alert_active(feed.key),
            }
        )
    return {
        "checked_at": now.isoformat(),
        "threshold_hours": s.ingest_stale_hours,
        "renotify_hours": s.ingest_stale_renotify_hours,
        "recipients": len(s.ops_alert_emails_list),
        "feeds": feeds,
    }


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


# ---------------------------------------------------------------------------
# check_station_exposure — is a weather station measuring the field?
# ---------------------------------------------------------------------------
# A station installed in a dark or closed space (Router02, 2026-11) sends
# plausible numbers that are not the field's: 0 W/m² at noon, 0 m/s all day,
# a damped temperature swing and warm nights. Once a day each station that
# reported in the last 24 h is compared with Open-Meteo at the farm's location
# (agri.core.station_exposure); a sheltered/suspect one is emailed to ops,
# at most once per EXPOSURE_RENOTIFY_DAYS while it lasts. The latest report per
# station is kept in Redis for GET /admin/monitoring/stations.
EXPOSURE_WINDOW_HOURS = 24
EXPOSURE_RENOTIFY_DAYS = 7
EXPOSURE_REPORT_TTL_S = 3 * 86400
_EXPOSURE_REPORT_PREFIX = "station-exposure:report:"
_EXPOSURE_ALERT_PREFIX = "station-exposure:alert:"
_EXPOSURE_TABLES = {
    "station_temp_c": "analytics_temperatureweather",
    "station_wind_ms": "analytics_windspeed",
    "station_rs_wm2": "analytics_solarradiation",
}
_SIGNAL_TEXT = {
    "no_sun": "solar sensor reads ~0 in daylight (max {station_max_wm2} W/m², "
    "outside up to {reference_max_wm2} W/m²)",
    "no_wind": "wind sensor reads 0 all day (outside averages "
    "{reference_mean_ms} m/s at 10 m)",
    "damped_temperature": "temperature swings {station_range_c} °C vs "
    "{reference_range_c} °C outside (low {station_min_c} vs {reference_min_c} °C)",
    "warm_nights": "nights {mean_bias_c} °C warmer than outside",
}


def _station_devices(session, start, end) -> list:
    """Stations with weather readings in ``[start, end)``: device id, its zone
    and owner (from the latest reading), serial and name."""
    return session.execute(
        text(
            "SELECT DISTINCT ON (t.device_id) t.device_id, t.zone_id, t.user_id, "
            "d.serial, d.name "
            "FROM analytics_temperatureweather t "
            "JOIN analytics_device d ON d.id = t.device_id "
            "WHERE t.device_id IS NOT NULL AND t.timestamp >= :a AND t.timestamp < :b "
            "ORDER BY t.device_id, t.timestamp DESC"
        ),
        {"a": start, "b": end},
    ).all()


def _hourly_means(session, table: str, device_id: int, start, end) -> dict[str, float]:
    rows = session.execute(
        text(
            f"SELECT date_trunc('hour', timestamp) AS h, avg(value) FROM {table} "
            "WHERE device_id = :d AND timestamp >= :a AND timestamp < :b "
            "GROUP BY 1"
        ),
        {"d": device_id, "a": start, "b": end},
    ).all()
    return {
        h.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H"): float(v)
        for h, v in rows
        if v is not None
    }


def _exposure_hours(station: dict[str, dict[str, float]], reference: dict) -> list:
    from agri.core.station_exposure import ExposureHour

    out = []
    for key in sorted(station["station_temp_c"]):
        ref = reference.get(key) or {}
        out.append(
            ExposureHour(
                hour=datetime.datetime.strptime(key, "%Y-%m-%dT%H").replace(
                    tzinfo=datetime.timezone.utc
                ),
                station_temp_c=station["station_temp_c"].get(key),
                station_wind_ms=station["station_wind_ms"].get(key),
                station_rs_wm2=station["station_rs_wm2"].get(key),
                ref_temp_c=ref.get("temp_c"),
                ref_wind_ms=ref.get("wind_ms"),
                ref_rs_wm2=ref.get("rs_wm2"),
            )
        )
    return out


def _store_exposure_report(device_id: int, report: dict) -> None:
    import json

    try:
        _redis().set(
            _EXPOSURE_REPORT_PREFIX + str(device_id),
            json.dumps(report, default=str),
            ex=EXPOSURE_REPORT_TTL_S,
        )
    except Exception:  # Redis down: the email path still works
        logger.warning("station_exposure: cannot store report for %s", device_id)


def stored_exposure_reports() -> list[dict]:
    """Latest report per station (for the admin monitoring page)."""
    import json

    try:
        r = _redis()
        keys = sorted(r.scan_iter(_EXPOSURE_REPORT_PREFIX + "*"))
        return [json.loads(v) for v in r.mget(keys) if v] if keys else []
    except Exception:
        logger.warning("station_exposure: redis unavailable for reports")
        return []


def _exposure_window() -> tuple[datetime.datetime, datetime.datetime]:
    end = _utcnow().replace(minute=0, second=0, microsecond=0)
    return end - datetime.timedelta(hours=EXPOSURE_WINDOW_HOURS), end


def _assess_station(session, dev, start, end, placement: str) -> dict:
    """Compare one station with the outdoor reference over ``[start, end)``
    and store the report. Returns it (``status`` is ``no_location`` /
    ``no_reference`` when it can't be judged)."""
    from agri.core.station_exposure import assess_station_exposure
    from apps.sensors.forecast_provider import fetch_openmeteo_hourly_reference

    from fastapp.sector_insights import zone_point
    from fastapp.station_placement import detected_placement, is_mismatch

    base = {
        "device_id": dev.device_id,
        "device": dev.name or dev.serial or f"device {dev.device_id}",
        "serial": dev.serial,
        "zone_id": dev.zone_id,
        "user_id": dev.user_id,
        "checked_at": end.isoformat(),
        "window_hours": EXPOSURE_WINDOW_HOURS,
        "placement": placement,
    }
    point = zone_point(session, dev.zone_id, dev.user_id)
    reference = None
    if point is not None:
        reference = fetch_openmeteo_hourly_reference(
            start=start.date(), end=end.date(), latitude=point[0], longitude=point[1]
        )
    if not reference:
        report = {
            **base,
            "status": "no_location" if point is None else "no_reference",
            "hours_compared": 0,
            "signals": [],
            "checked": [],
            "sensors": {},
            "detected": detected_placement(""),
            "mismatch": False,
        }
    else:
        station = {
            field: _hourly_means(session, table, dev.device_id, start, end)
            for field, table in _EXPOSURE_TABLES.items()
        }
        result = assess_station_exposure(_exposure_hours(station, reference))
        detected = detected_placement(result.status)
        report = {
            **base,
            **result.as_dict(),
            "detected": detected,
            "mismatch": is_mismatch(placement, detected),
        }
    _store_exposure_report(dev.device_id, report)
    return report


def _email_exposure(report: dict, recipients: list[str]) -> int:
    lines = [
        "- " + _SIGNAL_TEXT[sig["code"]].format(**sig["detail"])
        for sig in report["signals"]
        if sig["code"] in _SIGNAL_TEXT
    ]
    label = report["device"]
    subject = f"[Agrogo ops] Weather station {label} looks {report['status']}"
    declared = (
        " It was declared outdoors in the back-office."
        if report.get("placement") == "outdoor"
        else ""
    )
    body = (
        f"Station {label} (zone {report['zone_id']}) does not seem to measure the "
        f"field.{declared} Compared with outdoor weather at the farm over the last "
        f"{EXPOSURE_WINDOW_HOURS} h ({report['hours_compared']} hours):\n"
        + "\n".join(lines)
        + "\n\nA station in a closed, dark or sheltered spot sends readings "
        "that are not the field's: ET0, VPD and temperature alerts built on "
        "them are off. Move it outdoors, in the open, about 2 m high. If it is "
        "indoors on purpose, set it to 'indoor' in the back-office (Stations) "
        "to stop these emails.\n\n"
        f"You will be reminded every {EXPOSURE_RENOTIFY_DAYS} days while "
        "it stays like this."
    )
    return sum(_send_email(to=r, subject=subject, body=body) for r in recipients)


def check_station_exposure() -> dict:
    """Beat job: flag weather stations that look sheltered (see section doc).
    Stations declared ``indoor`` in the back-office are checked but never
    emailed about."""
    from agri.core.station_exposure import STATUS_SHELTERED, STATUS_SUSPECT

    from fastapp.station_placement import alerts_enabled, placement_of, read_placements

    s = get_settings()
    start, end = _exposure_window()
    results: dict[str, str] = {}
    alerted: list[str] = []

    with session_scope() as session:
        placements = read_placements(session)
        for dev in _station_devices(session, start, end):
            placement = placement_of(placements, dev.device_id)
            report = _assess_station(session, dev, start, end, placement)
            label = report["device"]
            results[label] = report["status"]
            if report["status"] not in (STATUS_SHELTERED, STATUS_SUSPECT):
                continue
            if not alerts_enabled(placement):
                continue
            recipients = s.ops_alert_emails_list
            ttl = EXPOSURE_RENOTIFY_DAYS * 86400
            try:
                claimed = bool(
                    _redis().set(
                        _EXPOSURE_ALERT_PREFIX + str(dev.device_id), 1, nx=True, ex=ttl
                    )
                )
            except Exception:
                logger.warning("station_exposure: redis unavailable, not alerting")
                claimed = False
            if not recipients or not claimed:
                continue
            sent = _email_exposure(report, recipients)
            logger.warning(
                "station_exposure: %s %s (%s), emailed %d",
                label,
                report["status"],
                ", ".join(sig["code"] for sig in report["signals"]),
                sent,
            )
            alerted.append(label)

    return {"checked_at": end.isoformat(), "stations": results, "alerted": alerted}


def check_station_now(device_id: int) -> dict | None:
    """Back-office "check now" for one device: same comparison as the daily
    job, report stored, no email. None when the device doesn't exist;
    ``status: no_readings`` when it sent no weather reading in the window."""
    from fastapp.station_placement import placement_of, read_placements

    start, end = _exposure_window()
    with session_scope() as session:
        dev = next(
            (
                d
                for d in _station_devices(session, start, end)
                if d.device_id == device_id
            ),
            None,
        )
        placement = placement_of(read_placements(session), device_id)
        if dev is not None:
            return _assess_station(session, dev, start, end, placement)
        row = session.execute(
            text(
                "SELECT id, serial, name, zone_id, user_id FROM analytics_device WHERE id = :d"
            ),
            {"d": device_id},
        ).first()
    if row is None:
        return None
    return {
        "device_id": row.id,
        "device": row.name or row.serial or f"device {row.id}",
        "serial": row.serial,
        "zone_id": row.zone_id,
        "user_id": row.user_id,
        "checked_at": end.isoformat(),
        "window_hours": EXPOSURE_WINDOW_HOURS,
        "placement": placement,
        "status": "no_readings",
        "hours_compared": 0,
        "signals": [],
        "checked": [],
        "sensors": {},
        "detected": "unknown",
        "mismatch": False,
    }


STATION_LIST_DAYS = 7


def list_stations() -> list[dict]:
    """Every weather station seen in the last ``STATION_LIST_DAYS`` days or with
    a declared placement, with its declaration and its latest report."""
    from fastapp.station_placement import is_mismatch, placement_of, read_placements

    end = _utcnow()
    reports = {r.get("device_id"): r for r in stored_exposure_reports()}
    with session_scope() as session:
        placements = read_placements(session)
        recent = session.execute(
            text(
                "SELECT t.device_id, max(t.timestamp) AS last_reading "
                "FROM analytics_temperatureweather t "
                "WHERE t.device_id IS NOT NULL AND t.timestamp >= :a "
                "GROUP BY t.device_id"
            ),
            {"a": end - datetime.timedelta(days=STATION_LIST_DAYS)},
        ).all()
        last = {r.device_id: r.last_reading for r in recent}
        ids = sorted(set(last) | set(placements) | {i for i in reports if i})
        devices = (
            session.execute(
                text(
                    "SELECT d.id, d.serial, d.name, d.zone_id, d.user_id, "
                    'u.username FROM analytics_device d LEFT JOIN "CustomUser_customuser" u '
                    "ON u.id = d.user_id WHERE d.id = ANY(:ids)"
                ),
                {"ids": ids},
            ).all()
            if ids
            else []
        )
    out = []
    for d in devices:
        placement = placement_of(placements, d.id)
        report = reports.get(d.id)
        if report is not None:
            # The declaration may have changed since the report was made.
            report = {
                **report,
                "placement": placement,
                "mismatch": is_mismatch(placement, report.get("detected", "unknown")),
            }
        ts = last.get(d.id)
        out.append(
            {
                "device_id": d.id,
                "device": d.name or d.serial or f"device {d.id}",
                "serial": d.serial,
                "zone_id": d.zone_id,
                "user_id": d.user_id,
                "username": d.username,
                "placement": placement,
                "placement_updated_at": (placements.get(d.id) or {}).get("updated_at"),
                "placement_updated_by": (placements.get(d.id) or {}).get("updated_by"),
                "last_reading": ts.isoformat() if ts else None,
                "report": report,
            }
        )
    return out
