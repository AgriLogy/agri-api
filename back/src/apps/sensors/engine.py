"""Django adapter around agri-core's hourly sensor aggregation.

The aggregation itself runs in ``agri.core`` — ``AgriMainDBClient.hourly_averages``
over the ``agri.db`` SQLAlchemy models. This module keeps the Django-coupled
bits, mirroring ``apps.alerts.engine``:

* resolve a Django sensor model to its ``agri.db.analytics`` counterpart,
* open an ``agri.core.database`` session and delegate the aggregation,
* re-attach the presentation metadata the core layer doesn't carry — the
  per-sensor units (``default_unit`` / ``available_units``, Django model
  properties) and the constant ``color`` / ``courbe_name`` text columns.

Per memory ``project_agri_core_architecture``: logic + DB access live in
agri-core; agri-api is the thin Django shell.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from django.db.models import CharField, FloatField
from django.utils import timezone as djtz

try:
    from agri.core.database.config import (
        DatabaseNotConfiguredError as _DB_NOT_CONFIGURED,
    )
except Exception:  # pragma: no cover - agri-core always ships config
    _DB_NOT_CONFIGURED = RuntimeError


def _orm_hourly_averages(
    model_cls,
    *,
    user_id: int,
    zone_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    value_columns: tuple[str, ...] = ("value",),
) -> list[dict[str, Any]]:
    """Clock-hour averages via the Django ORM (local-dev fallback).

    Returns the same bucket shape as ``AgriMainDBClient.hourly_averages``:
    ``{"hour": <aware datetime>, "last_id": <int>, <col>: <avg>}``.
    """
    qs = model_cls.objects.filter(user_id=user_id)
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)
    if start is not None:
        qs = qs.filter(timestamp__gte=start)
    if end is not None:
        qs = qs.filter(timestamp__lt=end)
    qs = qs.order_by("timestamp")

    groups: dict[datetime, dict[str, Any]] = {}
    for row in qs:
        ts = row.timestamp
        if ts is None:
            continue
        hour = ts.replace(minute=0, second=0, microsecond=0)
        g = groups.get(hour)
        if g is None:
            g = groups[hour] = {"sums": {}, "n": 0, "last_id": 0}
        g["n"] += 1
        g["last_id"] = max(g["last_id"], row.id or 0)
        for c in value_columns:
            v = getattr(row, c, None)
            if isinstance(v, (int, float)):
                g["sums"][c] = g["sums"].get(c, 0.0) + v
    buckets = []
    for hour in sorted(groups):
        g = groups[hour]
        b: dict[str, Any] = {"hour": hour, "last_id": g["last_id"]}
        for c in value_columns:
            b[c] = g["sums"][c] / g["n"] if c in g["sums"] else None
        buckets.append(b)
    return buckets


def value_fields(model_cls) -> list[str]:
    """Float measurement columns to average per hour — ``["value"]`` for most
    sensors, the three N/P/K columns for ``NpkSensor`` (no single ``value``).
    """
    return [f.name for f in model_cls._meta.get_fields() if isinstance(f, FloatField)]


def _meta_char_fields(model_cls) -> list[str]:
    """Constant per-sensor text columns (``color`` / ``courbe_name`` and the
    NPK per-component variants) carried through so chart legends keep working.
    """
    return [f.name for f in model_cls._meta.get_fields() if isinstance(f, CharField)]


def _agri_db_model(model_cls):
    """Resolve a Django sensor model to its ``agri.db.analytics`` ORM class.

    Mirrors ``agri.core.alerts.db_model_for``'s transform: the agri.db class
    is ``Analytics`` + the Django name lowercased then capitalised
    (``TemperatureWeather`` → ``AnalyticsTemperatureweather``).
    """
    import agri.db.analytics as analytics

    return getattr(analytics, "Analytics" + model_cls.__name__.lower().capitalize())


def hourly_readings(
    model_cls,
    *,
    user_id: int,
    zone_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    """One averaged reading per clock hour for ``user_id`` — computed by
    agri-core, serialized to the django-ninja sensors wire shape.

    ``start`` / ``end`` are inclusive *dates* (the endpoint's
    ``start_date`` / ``end_date`` filters); they're widened to a half-open
    ``[start 00:00, (end + 1 day) 00:00)`` datetime window for core.
    Returns ``[]`` when the user has no rows for this sensor.
    """
    from agri.core.database import AgriMainDBClient, session_scope

    cols = value_fields(model_cls)

    # Presentation metadata is constant per sensor/zone, so one Django row
    # supplies color/courbe_name + zone/user; units come from the model.
    sample_qs = model_cls.objects.filter(user_id=user_id)
    if zone_id is not None:
        sample_qs = sample_qs.filter(zone_id=zone_id)
    sample = sample_qs.first()
    if sample is None:
        return []

    meta = {m: getattr(sample, m, None) for m in _meta_char_fields(model_cls)}
    default_unit = getattr(sample, "default_unit", None)
    available_units = getattr(sample, "available_units", None)

    start_dt = djtz.make_aware(datetime.combine(start, time.min)) if start else None
    end_dt = (
        djtz.make_aware(datetime.combine(end + timedelta(days=1), time.min))
        if end
        else None
    )

    try:
        with session_scope() as session:
            buckets = AgriMainDBClient.hourly_averages(
                session,
                _agri_db_model(model_cls),
                user_id=user_id,
                zone_id=zone_id,
                start=start_dt,
                end=end_dt,
                value_columns=cols or ("value",),
            )
    except _DB_NOT_CONFIGURED:
        # Local-dev fallback (SQLite, no Postgres DSN): same wire shape,
        # averaged per clock hour with the Django ORM. Prod always has a
        # DSN, so this path never runs there.
        buckets = _orm_hourly_averages(
            model_cls,
            user_id=user_id,
            zone_id=zone_id,
            start=start_dt,
            end=end_dt,
            value_columns=cols or ("value",),
        )

    rows: list[dict[str, Any]] = []
    for b in buckets:
        ts: datetime | None = b["hour"]
        row: dict[str, Any] = dict(meta)
        row["id"] = b["last_id"]
        row["zone"] = sample.zone_id
        row["user"] = sample.user_id
        row["timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else None
        for c in cols:
            row[c] = b.get(c)
        row["default_unit"] = default_unit
        row["available_units"] = available_units
        rows.append(row)
    return rows
