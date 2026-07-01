"""Admin sensor-data backfill (django-ninja).

Mounted at ``/api/admin``. Lets a staff user fill the gap between a user/zone's
last real sensor reading and *now* with synthesized continuation data, so the
dashboard graphs render again — no SSH, no code, no per-table work.

  * GET  /admin/users/<username>/zones/<zone_id>/backfill-status
  * POST /admin/users/<username>/zones/<zone_id>/backfill

Generation strategy (generic, schema-introspected): every model that has
``user`` + ``zone`` + ``timestamp`` fields is a backfill target. For each, the
last real row is carried forward at a fixed interval, with light ±jitter on
numeric columns, skipping timestamps that already exist. Bulk-inserted under
safety caps. Staff-only; audited.
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import Any

from django.apps import apps as django_apps
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from apps.irrigation.audit import record_audit

router = Router()
log = logging.getLogger(__name__)

# Safety caps so a backfill can never run away.
_MAX_ROWS_PER_MODEL = 20_000
_MAX_ROWS_TOTAL = 250_000
_MIN_INTERVAL = 5
_MAX_INTERVAL = 1440

_NUMERIC_TYPES = {
    "FloatField",
    "DecimalField",
    "IntegerField",
    "BigIntegerField",
    "SmallIntegerField",
    "PositiveIntegerField",
    "PositiveSmallIntegerField",
    "PositiveBigIntegerField",
}


def _require_admin(request) -> Response | None:
    user = request.auth
    if user is None or not getattr(user, "is_staff", False):
        return Response({"detail": "Admin access required"}, status=403)
    return None


def _resolve(username: str, zone_id: int):
    """Return (user, zone) for an owned zone, or (None, None)."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(username=username).first()
    if user is None:
        return None, None
    Zone = django_apps.get_model("analytics", "zone")
    zone = Zone.objects.filter(pk=zone_id, user=user).first()
    return user, zone


def _backfillable_models() -> list[Any]:
    """Every model with user + zone + timestamp fields (i.e. sensor series)."""
    out = []
    for model in django_apps.get_models():
        names = {f.name for f in model._meta.concrete_fields}
        if {"user", "zone", "timestamp"} <= names:
            out.append(model)
    return out


def _copy_fields(model) -> list[Any]:
    skip = {"id", "timestamp", "user", "zone"}
    return [
        f
        for f in model._meta.concrete_fields
        if f.name not in skip and not f.primary_key
    ]


def _jitter(field, value):
    if value is None:
        return None
    if field.get_internal_type() in _NUMERIC_TYPES and not isinstance(value, bool):
        factor = 1.0 + random.uniform(-0.05, 0.05)
        new = value * factor
        if field.get_internal_type() in ("FloatField", "DecimalField"):
            return round(float(new), 4)
        return int(round(new))
    return value


def _last_data_ts(user, zone):
    """Latest existing timestamp across all backfillable models, or None."""
    newest = None
    for model in _backfillable_models():
        ts = (
            model.objects.filter(user=user, zone=zone)
            .aggregate(m=Max("timestamp"))
            .get("m")
        )
        if ts and (newest is None or ts > newest):
            newest = ts
    return newest


class BackfillIn(Schema):
    start: str | None = None  # ISO; default = last existing reading
    end: str | None = None  # ISO; default = now
    interval_minutes: int = 60
    dry_run: bool = False


@router.get(
    "/users/{username}/zones/{zone_id}/backfill-status",
    auth=JwtAuth(),
    summary="Admin: backfill readiness for a zone",
)
def backfill_status(request, username: str, zone_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user, zone = _resolve(username, zone_id)
    if zone is None:
        return Response({"detail": "Zone not found for this user."}, status=404)

    models = _backfillable_models()
    last = _last_data_ts(user, zone)
    now = timezone.now()
    with_data = 0
    for model in models:
        if model.objects.filter(user=user, zone=zone).exists():
            with_data += 1
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
    "/users/{username}/zones/{zone_id}/backfill",
    auth=JwtAuth(),
    summary="Admin: backfill a zone's sensor series up to now",
)
def backfill(request, username: str, zone_id: int, payload: BackfillIn):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    user, zone = _resolve(username, zone_id)
    if zone is None:
        return Response({"detail": "Zone not found for this user."}, status=404)

    interval = max(_MIN_INTERVAL, min(_MAX_INTERVAL, int(payload.interval_minutes)))
    step = timedelta(minutes=interval)

    end = parse_datetime(payload.end) if payload.end else None
    end = timezone.make_aware(end) if (end and timezone.is_naive(end)) else end
    end = end or timezone.now()

    start = parse_datetime(payload.start) if payload.start else None
    start = (
        timezone.make_aware(start) if (start and timezone.is_naive(start)) else start
    )
    start_auto = start is None
    # An explicit start must precede end; an auto (per-series) start is derived
    # below from each series' own last reading, so series can be at different
    # freshness and each still gets filled.
    if not start_auto and start > end:
        return Response({"detail": "start must be before end."}, status=400)

    models = _backfillable_models()
    if start_auto and not any(
        m.objects.filter(user=user, zone=zone).exists() for m in models
    ):
        return Response(
            {"detail": "This zone has no existing data to extend from."},
            status=400,
        )

    per_model: dict[str, int] = {}
    total = 0
    for model in models:
        last_row = (
            model.objects.filter(user=user, zone=zone).order_by("-timestamp").first()
        )
        if last_row is None:
            continue
        # Auto mode extends each series from its own last reading; explicit mode
        # uses the shared start for every series.
        model_start = (last_row.timestamp + step) if start_auto else start
        if model_start > end:
            continue
        copy = _copy_fields(model)
        existing = set(
            model.objects.filter(
                user=user, zone=zone, timestamp__gte=model_start, timestamp__lte=end
            ).values_list("timestamp", flat=True)
        )
        rows = []
        ts = model_start
        while ts <= end and len(rows) < _MAX_ROWS_PER_MODEL:
            if total + len(rows) >= _MAX_ROWS_TOTAL:
                break
            if ts not in existing:
                kwargs = {
                    f.attname: _jitter(f, getattr(last_row, f.attname)) for f in copy
                }
                rows.append(model(user=user, zone=zone, timestamp=ts, **kwargs))
            ts += step
        if rows and not payload.dry_run:
            model.objects.bulk_create(rows, batch_size=1000)
        if rows:
            per_model[model._meta.label_lower] = len(rows)
            total += len(rows)
        if total >= _MAX_ROWS_TOTAL:
            break

    if not payload.dry_run:
        record_audit(
            request.auth,
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
