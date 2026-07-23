"""fastapp /reports — the historical report (RPT-1, #399 / #441).

Two append-only histories, written by :mod:`fastapp.history`:

* ``analytics_alertevent``          — every alert firing, with the rule as it
  read at that moment (name / condition / threshold / unit), so the report
  stays truthful after the rule is edited or deleted;
* ``analytics_irrigationdecision``  — every computed recommendation, with the
  water-balance inputs it came from.

Both endpoints answer the same question shape — **date range + zone** — and are
owner-scoped. Same two constraints as ``routers/sensor_groups.py``:

1. **Raw SQL.** agri-api pins agri-core 0.23.0 → agri-db 0.17.0, which predates
   both tables; statements are parameterised ``text()``.
2. **The migration (``e7a1c3d5b209``) is not applied in production.** Both
   endpoints ask ``fastapp.schema_compat`` first and answer an EMPTY page with
   ``schema_available: false`` — never an ``UndefinedTable`` 500.

Scoping reuses ``routers/selfreads._resolve_read_scope``: a regular user sees
their own history; a technician sees only rows in the zones granted to them
(no grant → nothing).
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import bindparam, text

from agri.core.database import session_scope
from agri.db.users import CustomUserCustomuser
from fastapp.auth import AuthedUser, get_current_user
from fastapp.routers.selfreads import _ReadScope, _resolve_read_scope
from fastapp.schema_compat import (
    ALERT_EVENT_TABLE,
    IRRIGATION_DECISION_TABLE,
    alert_events_available,
    irrigation_decisions_available,
)

router = APIRouter(tags=["reports"])

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _scope(session, user_id: int) -> _ReadScope:
    row = session.get(CustomUserCustomuser, user_id)
    if row is None:  # pragma: no cover - a JWT for a deleted user
        return _ReadScope(owner_id=user_id, zone_ids=None)
    return _resolve_read_scope(session, row)


def _utc(value: datetime.datetime | None) -> datetime.datetime | None:
    """A naive bound (``?start=2026-07-01``) is UTC, like every stored stamp."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _empty(limit: int, offset: int) -> dict[str, Any]:
    """The degraded page: the history table is not on this deployment yet."""
    return {
        "count": 0,
        "limit": limit,
        "offset": offset,
        "schema_available": False,
        "results": [],
    }


def _page(limit: int | None, offset: int | None) -> tuple[int, int]:
    limit = DEFAULT_LIMIT if limit is None else limit
    return max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset or 0))


def _scope_filter(
    scope: _ReadScope,
    zone_id: int | None,
    where: list[str],
    params: dict[str, Any],
    expanding: list[str],
) -> bool:
    """Owner + zone narrowing. Returns False when the caller can see nothing.

    A technician is confined to the granted zones; an explicit ``zone_id`` may
    only narrow that set further, never widen it.
    """
    where.append("user_id = :owner")
    params["owner"] = scope.owner_id
    if scope.zone_ids is None:
        if zone_id is not None:
            where.append("zone_id = :zone")
            params["zone"] = zone_id
        return True
    allowed = scope.zone_ids
    if zone_id is not None:
        if not scope.zone_allowed(zone_id):
            return False
        allowed = {zone_id}
    if not allowed:
        return False
    where.append("zone_id IN :zones")
    params["zones"] = sorted(allowed)
    expanding.append("zones")
    return True


def _range_filter(
    start: datetime.datetime | None,
    end: datetime.datetime | None,
    column: str,
    where: list[str],
    params: dict[str, Any],
) -> None:
    """``[start, end]`` inclusive on both ends — a report reads as a period,
    not as a half-open interval."""
    if start is not None:
        where.append(f"{column} >= :start")
        params["start"] = _utc(start)
    if end is not None:
        where.append(f"{column} <= :end")
        params["end"] = _utc(end)


def _run(
    session,
    *,
    table: str,
    columns: str,
    order_by: str,
    where: list[str],
    params: dict[str, Any],
    expanding: list[str],
    limit: int,
    offset: int,
) -> tuple[int, list]:
    clause = " WHERE " + " AND ".join(where) if where else ""

    def _stmt(sql: str):
        stmt = text(sql)
        for name in expanding:
            stmt = stmt.bindparams(bindparam(name, expanding=True))
        return stmt

    total = session.execute(
        _stmt(f"SELECT count(*) FROM {table}{clause}"), params
    ).scalar_one()
    rows = session.execute(
        _stmt(
            f"SELECT {columns} FROM {table}{clause} "
            f"ORDER BY {order_by} DESC, id DESC LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": limit, "offset": offset},
    ).all()
    return total, rows


# ---------------------------------------------------------------------------
# /reports/alert-events
# ---------------------------------------------------------------------------
_ALERT_EVENT_COLUMNS = (
    "id, triggered_at, alert_id, user_id, zone_id, notification_zone_id, "
    "device_id, sensor_key, alert_name, condition, threshold_value, "
    "observed_value, reading_at, unit, notified_channels, context"
)


def _serialize_event(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "triggered_at": _iso(row.triggered_at),
        "alert_id": row.alert_id,
        "zone_id": row.zone_id,
        "notification_zone_id": row.notification_zone_id,
        "device_id": row.device_id,
        "sensor_key": row.sensor_key,
        # The rule AS IT WAS when it fired — not a lookup of today's rule.
        "alert_name": row.alert_name,
        "condition": row.condition,
        "threshold_value": row.threshold_value,
        "observed_value": row.observed_value,
        "reading_at": _iso(row.reading_at),
        "unit": row.unit,
        "notified_channels": (
            [c for c in (row.notified_channels or "").split(",") if c]
        ),
        "context": row.context,
    }


@router.get("/reports/alert-events", summary="Alert firings, newest first")
def list_alert_events(
    start: datetime.datetime | None = Query(
        default=None, description="Inclusive lower bound on triggered_at"
    ),
    end: datetime.datetime | None = Query(
        default=None, description="Inclusive upper bound on triggered_at"
    ),
    zone_id: int | None = None,
    sensor_key: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    user: AuthedUser = Depends(get_current_user),
):
    limit, offset = _page(limit, offset)
    with session_scope() as session:
        # COMPAT SHIM (#441): no table means no recorded history — an empty
        # page is the honest answer, never a 500.
        if not alert_events_available(session):
            return _empty(limit, offset)
        scope = _scope(session, user.id)
        where: list[str] = []
        params: dict[str, Any] = {}
        expanding: list[str] = []
        if not _scope_filter(scope, zone_id, where, params, expanding):
            return {
                "count": 0,
                "limit": limit,
                "offset": offset,
                "schema_available": True,
                "results": [],
            }
        _range_filter(start, end, "triggered_at", where, params)
        if sensor_key:
            where.append("sensor_key = :sensor_key")
            params["sensor_key"] = sensor_key
        total, rows = _run(
            session,
            table=ALERT_EVENT_TABLE,
            columns=_ALERT_EVENT_COLUMNS,
            order_by="triggered_at",
            where=where,
            params=params,
            expanding=expanding,
            limit=limit,
            offset=offset,
        )
        return {
            "count": total,
            "limit": limit,
            "offset": offset,
            "schema_available": True,
            "results": [_serialize_event(r) for r in rows],
        }


# ---------------------------------------------------------------------------
# /reports/irrigation-decisions
# ---------------------------------------------------------------------------
_DECISION_OUTCOME = (
    "id, decided_at, decision_date, user_id, zone_id, source, irrigate, "
    "reason, net_mm, gross_mm, volume_m3, duration_hr, morning_volume_m3, "
    "evening_volume_m3, capped_to_daily_max, summary"
)
_DECISION_INPUTS = (
    "dr_today_mm, raw_mm, taw_mm, et0_mm, kc_used, etc_mm, soil_moisture_pct, "
    "critical_moisture_pct, precipitation_forecast_mm, effective_rainfall_mm, "
    "zone_area_m2, flow_rate_m3h, max_water_per_day_m3, irrigation_efficiency, "
    "kr, context"
)


def _serialize_decision(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "decided_at": _iso(row.decided_at),
        "decision_date": _iso(row.decision_date),
        "zone_id": row.zone_id,
        "source": row.source,
        # what was recommended …
        "irrigate": row.irrigate,
        "reason": row.reason,
        "net_mm": row.net_mm,
        "gross_mm": row.gross_mm,
        "volume_m3": row.volume_m3,
        "duration_hr": row.duration_hr,
        "morning_volume_m3": row.morning_volume_m3,
        "evening_volume_m3": row.evening_volume_m3,
        "capped_to_daily_max": row.capped_to_daily_max,
        "summary": row.summary,
        # … and what it was computed from
        "inputs": {
            "dr_today_mm": row.dr_today_mm,
            "raw_mm": row.raw_mm,
            "taw_mm": row.taw_mm,
            "et0_mm": row.et0_mm,
            "kc_used": row.kc_used,
            "etc_mm": row.etc_mm,
            "soil_moisture_pct": row.soil_moisture_pct,
            "critical_moisture_pct": row.critical_moisture_pct,
            "precipitation_forecast_mm": row.precipitation_forecast_mm,
            "effective_rainfall_mm": row.effective_rainfall_mm,
            "zone_area_m2": row.zone_area_m2,
            "flow_rate_m3h": row.flow_rate_m3h,
            "max_water_per_day_m3": row.max_water_per_day_m3,
            "irrigation_efficiency": row.irrigation_efficiency,
            "kr": row.kr,
        },
        "context": row.context,
    }


@router.get(
    "/reports/irrigation-decisions",
    summary="Computed irrigation recommendations, newest first",
)
def list_irrigation_decisions(
    start: datetime.datetime | None = Query(
        default=None, description="Inclusive lower bound on decided_at"
    ),
    end: datetime.datetime | None = Query(
        default=None, description="Inclusive upper bound on decided_at"
    ),
    zone_id: int | None = None,
    source: str | None = None,
    irrigate: bool | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    user: AuthedUser = Depends(get_current_user),
):
    limit, offset = _page(limit, offset)
    with session_scope() as session:
        if not irrigation_decisions_available(session):
            return _empty(limit, offset)
        scope = _scope(session, user.id)
        where: list[str] = []
        params: dict[str, Any] = {}
        expanding: list[str] = []
        if not _scope_filter(scope, zone_id, where, params, expanding):
            return {
                "count": 0,
                "limit": limit,
                "offset": offset,
                "schema_available": True,
                "results": [],
            }
        _range_filter(start, end, "decided_at", where, params)
        if source:
            where.append("source = :source")
            params["source"] = source
        if irrigate is not None:
            where.append("irrigate = :irrigate")
            params["irrigate"] = irrigate
        total, rows = _run(
            session,
            table=IRRIGATION_DECISION_TABLE,
            columns=f"{_DECISION_OUTCOME}, {_DECISION_INPUTS}",
            order_by="decided_at",
            where=where,
            params=params,
            expanding=expanding,
            limit=limit,
            offset=offset,
        )
        return {
            "count": total,
            "limit": limit,
            "offset": offset,
            "schema_available": True,
            "results": [_serialize_decision(r) for r in rows],
        }
