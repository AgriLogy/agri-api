"""fastapp /admin sensor-data backfill (staff only).

Strangler port of ``apps/irrigation/router_backfill.py`` (django-ninja). Fills
the gap between a user/zone's last real sensor reading and *now* with
synthesized continuation data (light ±jitter carry-forward) so the dashboard
graphs render again. Reads/writes go through the agri-core SQLAlchemy session
(no Django ORM); the "backfillable" set is every agri.db model that carries
``user_id`` + ``zone_id`` + ``timestamp`` (the sensor series), mirroring the
Django ``_backfillable_models`` introspection.

Byte-parity note: both endpoints embed a live ``now`` (and the POST synthesizes
random-jittered values), so their *success* bodies are inherently
non-deterministic and NOT byte-compared with Django. The 404/400 envelopes ARE
byte-parity, and the deterministic counts (series totals, rows_created) match.
"""

from __future__ import annotations

import datetime
import random

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import BigInteger, Float, Integer, Numeric, func, select
from sqlalchemy.types import Boolean

import agri.db.analytics  # noqa: F401 — ensure sensor models are registered
from agri.core.database import session_scope
from agri.db.analytics import AnalyticsZone
from agri.db.base import AgriBase
from agri.db.users import CustomUserCustomuser
from fastapp.adminutil import record_audit
from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["admin-backfill"])

# Safety caps so a backfill can never run away (mirror Django).
_MAX_ROWS_PER_MODEL = 20_000
_MAX_ROWS_TOTAL = 250_000
_MIN_INTERVAL = 5
_MAX_INTERVAL = 1440

_NUMERIC_TYPES = (Integer, BigInteger, Float, Numeric)
_SKIP_COLS = {"id", "timestamp", "user_id", "zone_id"}


def _backfillable_models() -> list[type]:
    """Every mapped model with user_id + zone_id + timestamp columns (the sensor
    series), mirroring Django's ``_backfillable_models``."""
    out: list[type] = []
    for mapper in AgriBase.registry.mappers:
        cols = {c.key for c in mapper.columns}
        if {"user_id", "zone_id", "timestamp"} <= cols:
            out.append(mapper.class_)
    return out


def _label_lower(model: type) -> str:
    """Reproduce Django ``model._meta.label_lower`` for these tables:
    ``analytics.<tablename without the analytics_ prefix>``."""
    table = model.__tablename__
    return "analytics." + table[len("analytics_") :]


def _copy_columns(model: type) -> list:
    return [
        c
        for c in model.__table__.columns
        if c.key not in _SKIP_COLS and not c.primary_key
    ]


def _is_numeric(col) -> bool:
    return isinstance(col.type, _NUMERIC_TYPES) and not isinstance(col.type, Boolean)


def _jitter(col, value):
    if value is None:
        return None
    if _is_numeric(col) and not isinstance(value, bool):
        factor = 1.0 + random.uniform(-0.05, 0.05)
        new = value * factor
        if isinstance(col.type, (Float, Numeric)):
            return round(float(new), 4)
        return int(round(new))
    return value


def _resolve(session, username: str, zone_id: int):
    """Return (user, zone) for an owned zone, or (user_or_None, None)."""
    user = session.scalars(
        select(CustomUserCustomuser).where(CustomUserCustomuser.username == username)
    ).first()
    if user is None:
        return None, None
    zone = session.scalars(
        select(AnalyticsZone).where(
            AnalyticsZone.id == zone_id, AnalyticsZone.user_id == user.id
        )
    ).first()
    return user, zone


def _has_rows(session, model, user_id: int, zone_id: int) -> bool:
    return (
        session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.user_id == user_id, model.zone_id == zone_id)
        )
        or 0
    ) > 0


class BackfillIn(BaseModel):
    start: str | None = None  # ISO; default = last existing reading
    end: str | None = None  # ISO; default = now
    interval_minutes: int = 60
    dry_run: bool = False


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


@router.get(
    "/admin/users/{username}/zones/{zone_id}/backfill-status",
    summary="Admin: backfill readiness for a zone",
)
def backfill_status(
    username: str, zone_id: int, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope() as session:
        owner, zone = _resolve(session, username, zone_id)
        if zone is None:
            return DjangoStyleJSONResponse(
                {"detail": "Zone not found for this user."}, status_code=404
            )
        models = _backfillable_models()
        now = datetime.datetime.now(datetime.timezone.utc)
        last = None
        with_data = 0
        for model in models:
            ts = session.scalar(
                select(func.max(model.timestamp)).where(
                    model.user_id == owner.id, model.zone_id == zone.id
                )
            )
            if ts is not None:
                with_data += 1
                if last is None or ts > last:
                    last = ts
        gap_hours = round((now - last).total_seconds() / 3600, 1) if last else None
        return {
            "zone_id": zone_id,
            "zone_name": zone.name,
            "series_total": len(models),
            "series_with_data": with_data,
            "last_data_at": last.isoformat() if last else None,
            "now": now.isoformat(),
            "gap_hours": gap_hours,
        }


@router.post(
    "/admin/users/{username}/zones/{zone_id}/backfill",
    summary="Admin: backfill a zone's sensor series up to now",
)
def backfill(
    username: str,
    zone_id: int,
    payload: BackfillIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope(commit=True) as session:
        owner, zone = _resolve(session, username, zone_id)
        if zone is None:
            return DjangoStyleJSONResponse(
                {"detail": "Zone not found for this user."}, status_code=404
            )

        interval = max(_MIN_INTERVAL, min(_MAX_INTERVAL, int(payload.interval_minutes)))
        step = datetime.timedelta(minutes=interval)

        end = _parse_dt(payload.end) or datetime.datetime.now(datetime.timezone.utc)
        start = _parse_dt(payload.start)
        start_auto = start is None
        if not start_auto and start > end:
            return DjangoStyleJSONResponse(
                {"detail": "start must be before end."}, status_code=400
            )

        models = _backfillable_models()
        if start_auto and not any(
            _has_rows(session, m, owner.id, zone.id) for m in models
        ):
            return DjangoStyleJSONResponse(
                {"detail": "This zone has no existing data to extend from."},
                status_code=400,
            )

        per_model: dict[str, int] = {}
        total = 0
        for model in models:
            last_row = session.scalars(
                select(model)
                .where(model.user_id == owner.id, model.zone_id == zone.id)
                .order_by(model.timestamp.desc())
                .limit(1)
            ).first()
            if last_row is None:
                continue
            model_start = (last_row.timestamp + step) if start_auto else start
            if model_start > end:
                continue
            copy_cols = _copy_columns(model)
            existing = set(
                session.scalars(
                    select(model.timestamp).where(
                        model.user_id == owner.id,
                        model.zone_id == zone.id,
                        model.timestamp >= model_start,
                        model.timestamp <= end,
                    )
                ).all()
            )
            new_rows = []
            ts = model_start
            while ts <= end and len(new_rows) < _MAX_ROWS_PER_MODEL:
                if total + len(new_rows) >= _MAX_ROWS_TOTAL:
                    break
                if ts not in existing:
                    kwargs = {
                        c.key: _jitter(c, getattr(last_row, c.key)) for c in copy_cols
                    }
                    new_rows.append(
                        model(user_id=owner.id, zone_id=zone.id, timestamp=ts, **kwargs)
                    )
                ts += step
            if new_rows and not payload.dry_run:
                session.add_all(new_rows)
            if new_rows:
                per_model[_label_lower(model)] = len(new_rows)
                total += len(new_rows)
            if total >= _MAX_ROWS_TOTAL:
                break

        if not payload.dry_run:
            record_audit(
                session,
                user.id,
                "data.backfill",
                "zone",
                zone_id,
                {"username": username, "rows": total, "interval": interval},
            )

        return {
            "dry_run": payload.dry_run,
            "zone_id": zone_id,
            "start": start.isoformat() if start else "per-series",
            "end": end.isoformat(),
            "interval_minutes": interval,
            "rows_created": total,
            "per_series": per_model,
        }
