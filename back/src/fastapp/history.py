"""RPT-1 (#399 / #441) — persist WHAT HAPPENED, so a report can be read later.

Two things the platform computes many times a day and then throws away:

* an **alert firing** — today only the LAST one survives, on
  ``analytics_alert.last_triggered_at``;
* an **irrigation decision** — today the recommendation is rendered into an
  email / a chat answer and discarded.

This module appends one row per event to ``analytics_alertevent`` /
``analytics_irrigationdecision`` (agri-db migration ``e7a1c3d5b209``) and is
consumed by ``routers/reports.py``.

Three rules shape every line below.

1. **Raw SQL.** agri-api pins agri-core 0.23.0 → agri-db 0.17.0, which predates
   both tables, so there is no ORM model to import. Statements are
   parameterised ``text()``, the same idiom as ``routers/devices.py`` /
   ``routers/sensor_groups.py``.

2. **The migration is not applied in production.** Every entry point asks
   ``fastapp.schema_compat`` first (``alert_events_available`` /
   ``irrigation_decisions_available``, the SAME cached ``table_available``
   probe the sensor-group shim uses — not a second mechanism). Absent tables
   mean "record nothing"; the caller never notices.

3. **Recording must never break what it records.** This is observability
   bolted onto the two most load-bearing paths in the product — device ingest
   and the irrigation recommendation. Every write goes through
   :func:`best_effort`, which

   * runs the INSERT inside a SAVEPOINT (``session.begin_nested``) when it
     shares the caller's transaction, so a failed history INSERT cannot poison
     the surrounding one — without it Postgres aborts the whole transaction
     (``InFailedSqlTransaction``) and the READING ITSELF would be lost;
   * swallows every exception and logs a warning.

   The rule: an alert must still be sent and a recommendation must still be
   returned even when the history write fails.

The rule snapshot (``alert_name`` / ``condition`` / ``threshold_value`` /
``unit``) is copied INTO the event row at firing time on purpose: history must
keep reading correctly after the rule is edited or deleted (the FK is
``ON DELETE SET NULL``).
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
from typing import Any, Iterator

from sqlalchemy import text

from agri.core.database import session_scope
from fastapp.schema_compat import (
    ALERT_EVENT_TABLE,
    IRRIGATION_DECISION_TABLE,
    alert_events_available,
    irrigation_decisions_available,
)

log = logging.getLogger("fastapp.history")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# The best-effort wrapper — the whole safety story of this module.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def best_effort(what: str, *, session: Any = None) -> Iterator[None]:
    """Run a history write; never let it reach the caller.

    ``session`` — when the write shares the caller's transaction, pass it: the
    body runs inside a SAVEPOINT that is released on success and rolled back on
    failure, so the caller's own statements (the sensor reading, the alert
    cadence claim) survive a broken history INSERT. Postgres marks an entire
    transaction as aborted after ANY failed statement, so without the savepoint
    a history failure would take the reading down with it.
    """
    nested = None
    try:
        if session is not None:
            nested = session.begin_nested()
        yield
        if nested is not None:
            nested.commit()
    except Exception:  # noqa: BLE001 - deliberate: observability is never fatal
        if nested is not None:
            try:
                nested.rollback()
            except Exception:  # pragma: no cover - rollback of a dead savepoint
                log.warning("history: could not roll back the %s savepoint", what)
        log.warning("history: failed to record %s", what, exc_info=True)


def _clip(value: Any, width: int) -> str:
    """Fit a snapshot string into its column instead of 500-ing on overflow."""
    return ("" if value is None else str(value))[:width]


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str | None:
    if not value:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# analytics_alertevent
# ---------------------------------------------------------------------------
ALERT_EVENT_COLUMNS = (
    "triggered_at",
    "alert_id",
    "user_id",
    "zone_id",
    "notification_zone_id",
    "device_id",
    "sensor_key",
    "alert_name",
    "condition",
    "threshold_value",
    "observed_value",
    "reading_at",
    "unit",
    "notified_channels",
    "context",
)

_INSERT_ALERT_EVENT = text(
    f"INSERT INTO {ALERT_EVENT_TABLE} ("
    + ", ".join(ALERT_EVENT_COLUMNS)
    + ") VALUES ("
    + ", ".join(
        f"CAST(:{col} AS JSONB)" if col == "context" else f":{col}"
        for col in ALERT_EVENT_COLUMNS
    )
    + ") RETURNING id"
)


def build_alert_event_row(
    *,
    alert: Any,
    user_id: int,
    sensor_key: str,
    observed_value: float,
    reading_at: datetime.datetime | None,
    unit: str = "",
    zone_id: int | None = None,
    device_id: int | None = None,
    channels: list[str] | tuple[str, ...] = (),
    triggered_at: datetime.datetime | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One ``analytics_alertevent`` row, as a pure dict. No database.

    ``alert_name`` / ``condition`` / ``threshold_value`` / ``unit`` are COPIED
    off the rule here — the event is the record of what the rule said at the
    moment it fired, not a pointer to what the rule says today.

    ``zone_id`` is the rule's own zone when it has one, else the zone the
    READING came from: a user-wide rule still files its firing under the zone
    it actually fired on, which is what the report filters by.
    """
    return {
        "triggered_at": triggered_at or _utcnow(),
        "alert_id": getattr(alert, "id", None),
        "user_id": user_id,
        "zone_id": getattr(alert, "zone_id", None) or zone_id,
        "notification_zone_id": getattr(alert, "notification_zone_id", None),
        "device_id": device_id,
        "sensor_key": _clip(sensor_key, 64),
        "alert_name": _clip(getattr(alert, "name", ""), 200),
        "condition": _clip(getattr(alert, "condition", ""), 1),
        "threshold_value": _float(getattr(alert, "condition_nbr", None)) or 0.0,
        "observed_value": _float(observed_value) or 0.0,
        "reading_at": reading_at,
        "unit": _clip(unit, 32),
        "notified_channels": _clip(",".join(channels), 64),
        "context": _json(context),
    }


def _insert_guarded(
    session: Any,
    statement: Any,
    row: dict[str, Any],
    *,
    what: str,
    available: Any,
) -> None:
    """Probe, then INSERT — both best-effort, neither ever fatal.

    The probe runs OUTSIDE the savepoint so that a deployment without the
    tables (production today) pays nothing but the cached lookup; the INSERT
    runs INSIDE one so a failure cannot abort the caller's transaction.
    """
    with best_effort(what):
        if not available(session):
            return
        with best_effort(what, session=session):
            session.execute(statement, row)


def record_alert_event(session: Any, **kwargs: Any) -> None:
    """Append one alert-event row. Never raises, never blocks the alert.

    Shares the caller's transaction (the ingest write) behind a SAVEPOINT, so
    the reading and the Celery enqueue are unaffected by any failure here.
    """
    row: dict[str, Any] | None = None
    with best_effort("alert event"):
        row = build_alert_event_row(**kwargs)
    if row is None:  # the row builder itself failed; already logged
        return
    _insert_guarded(
        session,
        _INSERT_ALERT_EVENT,
        row,
        what="alert event",
        available=alert_events_available,
    )


# ---------------------------------------------------------------------------
# analytics_irrigationdecision
# ---------------------------------------------------------------------------
IRRIGATION_DECISION_COLUMNS = (
    "decided_at",
    "decision_date",
    "user_id",
    "zone_id",
    "source",
    "irrigate",
    "reason",
    "net_mm",
    "gross_mm",
    "volume_m3",
    "duration_hr",
    "morning_volume_m3",
    "evening_volume_m3",
    "capped_to_daily_max",
    "summary",
    "dr_today_mm",
    "raw_mm",
    "taw_mm",
    "et0_mm",
    "kc_used",
    "etc_mm",
    "soil_moisture_pct",
    "critical_moisture_pct",
    "precipitation_forecast_mm",
    "effective_rainfall_mm",
    "zone_area_m2",
    "flow_rate_m3h",
    "max_water_per_day_m3",
    "irrigation_efficiency",
    "kr",
    "context",
)

_INSERT_IRRIGATION_DECISION = text(
    f"INSERT INTO {IRRIGATION_DECISION_TABLE} ("
    + ", ".join(IRRIGATION_DECISION_COLUMNS)
    + ") VALUES ("
    + ", ".join(
        f"CAST(:{col} AS JSONB)" if col == "context" else f":{col}"
        for col in IRRIGATION_DECISION_COLUMNS
    )
    + ") RETURNING id"
)

#: ``decision_reason`` values from agri-core that mean "water now".
IRRIGATE_REASONS = frozenset({"stress", "soil_moisture_low", "complementary"})


def _decision_base(
    *,
    user_id: int,
    zone_id: int,
    source: str,
    decided_at: datetime.datetime | None,
) -> dict[str, Any]:
    """Every column at its NOT NULL-safe default, so a builder only has to fill
    what it actually knows — a missing key can never desync from the INSERT."""
    moment = decided_at or _utcnow()
    row: dict[str, Any] = {col: None for col in IRRIGATION_DECISION_COLUMNS}
    row.update(
        {
            "decided_at": moment,
            "decision_date": moment.date(),
            "user_id": user_id,
            "zone_id": zone_id,
            "source": _clip(source, 20),
            "irrigate": False,
            "reason": "",
            "net_mm": 0.0,
            "gross_mm": 0.0,
            "volume_m3": 0.0,
            "duration_hr": 0.0,
            "capped_to_daily_max": False,
            "summary": "",
            "precipitation_forecast_mm": 0.0,
        }
    )
    return row


def build_decision_row_from_advice(
    *,
    user_id: int,
    zone_id: int,
    source: str,
    advice: dict[str, Any],
    decision_reason: str | None = None,
    decided_at: datetime.datetime | None = None,
) -> dict[str, Any]:
    """One row from the assistant's ``_get_irrigation_advice`` payload.

    The payload is a rendered recommendation, not the raw dataclass: the
    depths it does not carry (``net_mm`` / ``gross_mm``) stay at 0 and the
    French sentence lands in ``summary``, while ``reason`` keeps the machine
    value the report groups by.
    """
    recommendation = advice.get("recommendation")
    row = _decision_base(
        user_id=user_id, zone_id=zone_id, source=source, decided_at=decided_at
    )
    duration_min = _float(advice.get("estimated_duration_min"))
    row.update(
        {
            "irrigate": recommendation == "irrigate",
            "reason": _clip(decision_reason or recommendation or "unknown", 32),
            "volume_m3": _float(advice.get("estimated_water_m3")) or 0.0,
            "duration_hr": (duration_min / 60.0) if duration_min else 0.0,
            "morning_volume_m3": _float(advice.get("morning_volume_m3")),
            "evening_volume_m3": _float(advice.get("evening_volume_m3")),
            "summary": advice.get("reason") or "",
            "dr_today_mm": _float(advice.get("dr_today_mm")),
            "raw_mm": _float(advice.get("raw_mm")),
            "et0_mm": _float(advice.get("et0_mm")),
            "soil_moisture_pct": _float(advice.get("soil_moisture_pct")),
            "critical_moisture_pct": _float(advice.get("critical_moisture_threshold")),
            "zone_area_m2": _float(advice.get("zone_area_m2")),
            "context": _json(
                {
                    "decision_source": advice.get("decision_source"),
                    "recommendation": recommendation,
                    "vpd_kpa": advice.get("vpd_kpa"),
                    "zone_name": advice.get("zone_name"),
                }
            ),
        }
    )
    return row


def build_decision_row_from_snapshot(
    *,
    user_id: int,
    zone_id: int,
    source: str,
    snapshot: dict[str, Any],
    precipitation_forecast_mm: float = 0.0,
    decided_at: datetime.datetime | None = None,
) -> dict[str, Any]:
    """One row from an ``agri.core.agronomy.field_snapshot`` dict (the daily
    notification path), which carries the water-balance inputs the assistant
    payload drops."""
    row = _decision_base(
        user_id=user_id, zone_id=zone_id, source=source, decided_at=decided_at
    )
    reason = snapshot.get("decision_reason")
    duration_min = _float(snapshot.get("recommended_duration_min"))
    et0 = _float(snapshot.get("et0_today_mm"))
    kc = _float(snapshot.get("kc_used"))
    row.update(
        {
            "irrigate": reason in IRRIGATE_REASONS,
            "reason": _clip(reason or "unknown", 32),
            "volume_m3": _float(snapshot.get("recommended_volume_m3")) or 0.0,
            "duration_hr": (duration_min / 60.0) if duration_min else 0.0,
            "morning_volume_m3": _float(snapshot.get("morning_volume_m3")),
            "evening_volume_m3": _float(snapshot.get("evening_volume_m3")),
            "summary": snapshot.get("irrigation_decision") or "",
            "dr_today_mm": _float(snapshot.get("dr_today_mm")),
            "raw_mm": _float(snapshot.get("raw_mm")),
            "taw_mm": _float(snapshot.get("taw_mm")),
            "et0_mm": et0,
            "kc_used": kc,
            "etc_mm": (et0 * kc) if (et0 is not None and kc is not None) else None,
            "soil_moisture_pct": _float(snapshot.get("soil_moisture_pct")),
            "precipitation_forecast_mm": _float(precipitation_forecast_mm) or 0.0,
            "context": _json({"zone_name": snapshot.get("zone_name")}),
        }
    )
    return row


def record_irrigation_decision(session: Any, row: dict[str, Any]) -> None:
    """Append one decision row on the caller's session. Never raises."""
    _insert_guarded(
        session,
        _INSERT_IRRIGATION_DECISION,
        row,
        what="irrigation decision",
        available=irrigation_decisions_available,
    )


def record_notification_decision(
    session: Any,
    user_id: int,
    *,
    source: str,
    now: datetime.datetime | None = None,
) -> None:
    """Record the decision behind a field-status notification.

    ``agri.core.notifications.compose_notification_for_user`` renders the
    snapshot straight into the email body and returns only a string, so the
    snapshot is recomputed here. That recomputation happens ONLY once the table
    probe says the history table exists — on production's current schema this
    function is a cached boolean check and nothing else. The caller must own a
    committing session; everything is best-effort.
    """
    with best_effort("irrigation decision"):
        if not irrigation_decisions_available(session):
            return
        from agri.core.agronomy import field_snapshot_for_user

        # The notification snapshot is built for the user's lowest-id zone
        # (agri-core's dashboard default) — mirror that choice here.
        zone_id = session.execute(
            text(
                "SELECT id FROM analytics_zone WHERE user_id = :uid ORDER BY id LIMIT 1"
            ),
            {"uid": user_id},
        ).scalar()
        if zone_id is None:  # no zone → no decision to record
            return
        snapshot = field_snapshot_for_user(session, user_id, now=now)
        row = build_decision_row_from_snapshot(
            user_id=user_id,
            zone_id=zone_id,
            source=source,
            snapshot=snapshot,
            decided_at=now,
        )
        record_irrigation_decision(session, row)


def record_advice_decision(**kwargs: Any) -> None:
    """Build a decision row from an assistant advice payload and append it in
    its own transaction. Row-building is inside the wrapper too, so even a
    malformed payload cannot reach the caller."""
    row: dict[str, Any] | None = None
    with best_effort("irrigation decision"):
        row = build_decision_row_from_advice(**kwargs)
    if row is None:  # the row builder itself failed; already logged
        return
    record_irrigation_decision_standalone(row)


def record_irrigation_decision_standalone(row: dict[str, Any]) -> None:
    """Append one decision row in its OWN committed transaction.

    For callers whose session is read-only (the assistant tool computes its
    advice inside ``session_scope()`` without commit): opening a short write
    scope here keeps the recommendation path untouched. Never raises — the
    session bring-up is inside the wrapper too.
    """
    with best_effort("irrigation decision"):
        with session_scope(commit=True) as session:
            record_irrigation_decision(session, row)


__all__ = [
    "ALERT_EVENT_COLUMNS",
    "IRRIGATE_REASONS",
    "IRRIGATION_DECISION_COLUMNS",
    "best_effort",
    "build_alert_event_row",
    "build_decision_row_from_advice",
    "build_decision_row_from_snapshot",
    "record_advice_decision",
    "record_alert_event",
    "record_notification_decision",
    "record_irrigation_decision",
    "record_irrigation_decision_standalone",
]
