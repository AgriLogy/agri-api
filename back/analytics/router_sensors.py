"""Auto-generated sensor read endpoints (django-ninja).

Replaces the ``generated_views`` loop in ``analytics.sensor_registry``
that built one DRF ``APIView`` per entry in
``agri.core.alerts.SENSOR_KEY_REGISTRY``-equivalent list (the older
``analytics.sensor_registry.SENSOR_MODELS``).

For every model M in ``SENSOR_MODELS`` we register a ninja route at::

    GET    /api/sensors/<slug>/           list the caller's readings
    PATCH  /api/sensors/<slug>/           partial update by ``id`` body

…where ``<slug>`` is the legacy ``name.lower().replace('sensor', '')
.replace('_', '-')`` derivation, so existing dashboard fetches keep
matching.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from django.forms.models import model_to_dict
from django.utils.dateparse import parse_date
from ninja import Router, Schema
from ninja.responses import Response

from agriBack.api.auth import JwtAuth
from analytics.sensor_registry import SENSOR_MODELS

router = Router()


def _serialize_reading(obj) -> dict[str, Any]:
    """Match ``BaseSensorSerializer``: every model field + computed
    ``default_unit`` / ``available_units`` + ISO timestamp format.
    """
    d = model_to_dict(obj)
    d["id"] = obj.pk
    ts: datetime | None = getattr(obj, "timestamp", None)
    if ts is not None:
        d["timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    d["default_unit"] = getattr(obj, "default_unit", None)
    d["available_units"] = getattr(obj, "available_units", None)
    return d


class _SensorPatchIn(Schema):
    """Partial-update body. ``id`` selects the row; other fields are
    free-form (the legacy DRF view used the model serializer with
    ``partial=True``).
    """

    id: int


def _slug_for(model_cls) -> str:
    name = model_cls.__name__
    return name.lower().replace("sensor", "").replace("_", "-")


def _build_list_handler(model_cls):
    def list_readings(
        request,
        start_date: str | None = None,
        end_date: str | None = None,
        zone: int | None = None,
    ):
        qs = model_cls.objects.filter(user=request.auth)
        if start_date:
            qs = qs.filter(timestamp__date__gte=parse_date(start_date))
        if end_date:
            qs = qs.filter(timestamp__date__lte=parse_date(end_date))
        if zone is not None:
            qs = qs.filter(zone_id=zone)
        return [_serialize_reading(r) for r in qs]

    list_readings.__name__ = f"list_{model_cls.__name__}"
    list_readings.__doc__ = (
        f"List the caller's {model_cls.__name__} readings (optional "
        f"start_date/end_date/zone filters)."
    )
    return list_readings


def _build_patch_handler(model_cls):
    def patch_reading(request, payload: _SensorPatchIn):
        try:
            obj = model_cls.objects.get(id=payload.id, user=request.auth)
        except model_cls.DoesNotExist:
            return Response({"error": "Not found"}, status=404)
        # Apply any extra fields the client sent (other than id).
        body = request.json if hasattr(request, "json") else None
        if body is None:
            try:
                import json as _json

                body = _json.loads(request.body or b"{}")
            except (ValueError, AttributeError):
                body = {}
        for k, v in (body or {}).items():
            if k == "id":
                continue
            if hasattr(obj, k):
                setattr(obj, k, v)
        obj.save()
        return _serialize_reading(obj)

    patch_reading.__name__ = f"patch_{model_cls.__name__}"
    patch_reading.__doc__ = (
        f"Partial-update one {model_cls.__name__} row by id (caller must own it)."
    )
    return patch_reading


# Register one GET + one PATCH per sensor model. The slugs match the
# legacy `generated_views` URL derivation.
for _model in SENSOR_MODELS:
    _slug = _slug_for(_model)
    router.add_api_operation(
        f"/sensors/{_slug}/",
        ["GET"],
        _build_list_handler(_model),
        auth=JwtAuth(),
        summary=f"List {_model.__name__} readings",
        tags=["sensors"],
    )
    router.add_api_operation(
        f"/sensors/{_slug}/",
        ["PATCH"],
        _build_patch_handler(_model),
        auth=JwtAuth(),
        summary=f"Patch one {_model.__name__} row by id",
        tags=["sensors"],
    )
