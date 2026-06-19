"""Admin sensor-data explorer (django-ninja) — mounted at ``/admin/sensor-data``.

A single generic surface over the 45 sensor reading models (``SENSOR_MODELS``)
so the back-office can inspect, correct and clean raw time-series for ANY
user/zone without 45 separate CRUD pages. All routes require JWT + ``is_staff``;
mutations are audited.

Endpoints:
  * GET    /admin/sensor-data/catalog            — sensor slugs + labels for the picker
  * GET    /admin/sensor-data?sensor=&username=&zone_id=&from=&to=&limit=
  * PATCH  /admin/sensor-data/{sensor}/{row_id}  — correct one reading's value
  * DELETE /admin/sensor-data/{sensor}/{row_id}  — delete one reading
  * DELETE /admin/sensor-data/{sensor}?zone_id=&from=&to=&username=
                                                 — bulk-delete a time range (guarded)

Note: NpkSensor stores three component values (N/P/K) rather than a single
``value`` — it appears in the catalog but its rows return ``value: null`` and
are not editable through this generic value-only explorer.
"""

from __future__ import annotations

from datetime import datetime

from django.contrib.auth import get_user_model
from django.utils.dateparse import parse_date, parse_datetime
from ninja import Query, Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from apps.irrigation.audit import record_audit
from apps.irrigation.router_admin import _require_admin
from apps.sensors.registry import SENSOR_MODELS
from apps.sensors.router_sensors import _slug_for

router = Router()
User = get_user_model()

SLUG_TO_MODEL = {_slug_for(m): m for m in SENSOR_MODELS}
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000


def _model_for(sensor: str):
    return SLUG_TO_MODEL.get(sensor)


def _humanize(name: str) -> str:
    """`SoilMoistureHigh` -> `Soil Moisture High`."""
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper():
            out.append(" ")
        out.append(ch)
    return "".join(out)


def _apply_range(qs, frm: str | None, to: str | None):
    if frm:
        dt = parse_datetime(frm)
        qs = (
            qs.filter(timestamp__gte=dt)
            if dt
            else qs.filter(timestamp__date__gte=parse_date(frm))
        )
    if to:
        dt = parse_datetime(to)
        qs = (
            qs.filter(timestamp__lte=dt)
            if dt
            else qs.filter(timestamp__date__lte=parse_date(to))
        )
    return qs


def _row(obj) -> dict:
    ts: datetime | None = getattr(obj, "timestamp", None)
    return {
        "id": obj.pk,
        "value": getattr(obj, "value", None),
        "timestamp": ts.isoformat() if ts else None,
        "zone_id": obj.zone_id,
        "username": getattr(obj.user, "username", None),
    }


class ValueIn(Schema):
    value: float | None = None


@router.get("/catalog", auth=JwtAuth(), summary="Admin: sensor catalog (slug + label)")
def catalog(request):
    guard = _require_admin(request)
    if guard:
        return guard
    return {
        "sensors": [
            {"slug": _slug_for(m), "label": _humanize(m.__name__)}
            for m in SENSOR_MODELS
        ]
    }


@router.get("", auth=JwtAuth(), summary="Admin: list sensor readings")
def list_readings(
    request,
    sensor: str,
    username: str | None = None,
    zone_id: int | None = None,
    frm: str | None = Query(None, alias="from"),
    to: str | None = None,
    limit: int = DEFAULT_LIMIT,
):
    guard = _require_admin(request)
    if guard:
        return guard
    model = _model_for(sensor)
    if model is None:
        return Response({"detail": "Unknown sensor"}, status=404)
    limit = max(1, min(limit, MAX_LIMIT))
    qs = model.objects.all().select_related("user")
    if username:
        qs = qs.filter(user__username=username)
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)
    qs = _apply_range(qs, frm, to).order_by("-timestamp")
    rows = [_row(o) for o in qs[:limit]]
    return {"sensor": sensor, "count": len(rows), "rows": rows}


@router.patch("/{sensor}/{row_id}", auth=JwtAuth(), summary="Admin: correct a reading")
def patch_reading(request, sensor: str, row_id: int, payload: ValueIn):
    guard = _require_admin(request)
    if guard:
        return guard
    model = _model_for(sensor)
    if model is None:
        return Response({"detail": "Unknown sensor"}, status=404)
    obj = model.objects.filter(pk=row_id).first()
    if obj is None:
        return Response({"detail": "Not found"}, status=404)
    if not hasattr(obj, "value"):
        return Response({"detail": "Sensor has no editable value"}, status=400)
    obj.value = payload.value
    obj.save(update_fields=["value"])
    record_audit(
        request.auth, "sensor_data.update", sensor, row_id, {"value": payload.value}
    )
    return _row(obj)


@router.delete(
    "/{sensor}/{row_id}", auth=JwtAuth(), summary="Admin: delete one reading"
)
def delete_reading(request, sensor: str, row_id: int):
    guard = _require_admin(request)
    if guard:
        return guard
    model = _model_for(sensor)
    if model is None:
        return Response({"detail": "Unknown sensor"}, status=404)
    deleted, _ = model.objects.filter(pk=row_id).delete()
    if not deleted:
        return Response({"detail": "Not found"}, status=404)
    record_audit(request.auth, "sensor_data.delete", sensor, row_id)
    return {"status": "deleted"}


@router.delete("/{sensor}", auth=JwtAuth(), summary="Admin: bulk-delete a range")
def delete_range(
    request,
    sensor: str,
    username: str | None = None,
    zone_id: int | None = None,
    frm: str | None = Query(None, alias="from"),
    to: str | None = None,
):
    guard = _require_admin(request)
    if guard:
        return guard
    model = _model_for(sensor)
    if model is None:
        return Response({"detail": "Unknown sensor"}, status=404)
    # Guard against nuking a whole sensor: require a zone AND a time bound.
    if zone_id is None or (not frm and not to):
        return Response(
            {"detail": "zone_id and at least one of from/to are required"},
            status=400,
        )
    qs = model.objects.filter(zone_id=zone_id)
    if username:
        qs = qs.filter(user__username=username)
    qs = _apply_range(qs, frm, to)
    deleted, _ = qs.delete()
    record_audit(
        request.auth,
        "sensor_data.range_delete",
        sensor,
        zone_id,
        {"from": frm, "to": to, "username": username, "deleted": deleted},
    )
    return {"status": "deleted", "deleted": deleted}
