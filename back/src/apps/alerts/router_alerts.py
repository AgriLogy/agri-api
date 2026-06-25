"""Alerts CRUD endpoints (django-ninja).

Migrated from ``analytics.views``:
  * GET, POST   /api/alert/
  * GET, PUT, PATCH, DELETE  /api/alert/<pk>/
  * GET   /api/alerts/for-graph/?sensor_key=&zone_id=
  * GET   /api/alerts/sensor-keys/
  * GET   /api/alerts/suggest/?sensor_key=&zone_id=

Schemas are loose (``dict`` returns) where the legacy DRF serializer
already produces a stable shape the dashboard parses; we'd lose more
typing energy than we'd gain by enumerating every Alert field.
"""

from __future__ import annotations

from typing import Any

from django.forms.models import model_to_dict
from ninja import Router, Schema
from ninja.responses import Response

from agri.core.alerts import SENSOR_KEY_REGISTRY
from agriapi.api.auth import JwtAuth
from agriapi.api.scope import block_if_technician
from apps.alerts.engine import recent_triggers_for_user, suggest_alert
from analytics.models import Alert, Zone

router = Router()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AlertWriteIn(Schema):
    """Request body for alert create/update (PUT/PATCH).

    All fields are optional so PATCH semantics work; POST validation falls
    back to model-level checks (required: name + condition + condition_nbr +
    sensor_key).
    """

    name: str | None = None
    type: str | None = None
    description: str | None = None
    condition: str | None = None
    condition_nbr: float | None = None
    sensor_key: str | None = None
    zone: int | None = None
    is_active: bool | None = None
    notify_email: bool | None = None
    notify_whatsapp: bool | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(alert: Alert) -> dict[str, Any]:
    """Match ``AlertSerializer`` shape (fields + computed threshold)."""
    d = model_to_dict(
        alert,
        fields=[
            "id",
            "name",
            "type",
            "description",
            "condition",
            "condition_nbr",
            "sensor_key",
            "zone",
            "is_active",
            "notify_email",
            "notify_whatsapp",
        ],
    )
    d["threshold"] = (
        float(alert.condition_nbr) if alert.condition_nbr is not None else None
    )
    d["last_triggered_at"] = (
        alert.last_triggered_at.isoformat() if alert.last_triggered_at else None
    )
    d["created_at"] = alert.created_at.isoformat() if alert.created_at else None
    d["updated_at"] = alert.updated_at.isoformat() if alert.updated_at else None
    d["user"] = alert.user_id
    # zone is already an int (FK id) via model_to_dict, leave as-is
    return d


def _apply(alert: Alert, payload: AlertWriteIn) -> Alert:
    data = payload.model_dump(exclude_unset=True)
    # `zone` arrives as an FK id; assign it to the *_id column rather than the
    # relation attribute (which would require a Zone instance).
    if "zone" in data:
        alert.zone_id = data.pop("zone")
    for k, v in data.items():
        setattr(alert, k, v)
    return alert


def _validate_write(request, payload: AlertWriteIn) -> Response | None:
    """Reject a write referencing an unknown sensor_key or a zone the caller
    doesn't own. Returns a 400 ``Response`` on failure, else ``None``.

    (Restores the validation the legacy DRF serializer enforced; the initial
    ninja migration dropped it.)
    """
    if payload.sensor_key is not None and payload.sensor_key not in SENSOR_KEY_REGISTRY:
        return Response(
            {"detail": f"Unknown sensor_key '{payload.sensor_key}'."}, status=400
        )
    if (
        payload.zone is not None
        and not Zone.objects.filter(id=payload.zone, user=request.auth).exists()
    ):
        return Response(
            {"detail": "zone not found or not owned by the caller."}, status=400
        )
    return None


# ---------------------------------------------------------------------------
# /api/alert/
# ---------------------------------------------------------------------------


@router.get("", auth=JwtAuth(), summary="List the caller's alerts")
def list_alerts(
    request,
    sensor_key: str | None = None,
    zone_id: int | None = None,
):
    qs = Alert.objects.filter(user=request.auth).order_by("-id")
    if sensor_key:
        qs = qs.filter(sensor_key=sensor_key)
    if zone_id is not None:
        qs = qs.filter(zone_id=zone_id)
    return [_serialize(a) for a in qs]


@router.post("", auth=JwtAuth(), summary="Create an alert for the caller")
def create_alert(request, payload: AlertWriteIn):
    blocked = block_if_technician(request)
    if blocked is not None:
        return blocked
    if not payload.name or not payload.condition or payload.condition_nbr is None:
        return Response(
            {"detail": "name, condition, and condition_nbr are required."},
            status=400,
        )
    err = _validate_write(request, payload)
    if err is not None:
        return err
    alert = Alert(user=request.auth)
    _apply(alert, payload)
    alert.save()
    return Response(_serialize(alert), status=201)


# ---------------------------------------------------------------------------
# Sub-collections under /alerts/ MUST be registered before /{pk} so the
# dynamic path doesn't shadow them.
# ---------------------------------------------------------------------------


@router.get(
    "/for-graph",
    auth=JwtAuth(),
    summary="Alerts annotated for chart overlays",
)
def alerts_for_graph(
    request,
    sensor_key: str | None = None,
    zone_id: int | None = None,
):
    if sensor_key and sensor_key not in SENSOR_KEY_REGISTRY:
        return Response(
            {"detail": f"Unknown sensor_key '{sensor_key}'."},
            status=400,
        )
    rows = recent_triggers_for_user(
        request.auth,
        sensor_key=sensor_key,
        zone_id=zone_id,
    )
    return {"alerts": rows}


@router.get(
    "/suggest",
    auth=JwtAuth(),
    summary="Suggested alert payload (mean-based prefill)",
)
def alerts_suggest(
    request,
    sensor_key: str | None = None,
    zone_id: int | None = None,
):
    if not sensor_key:
        return Response({"detail": "sensor_key is required."}, status=400)
    if sensor_key not in SENSOR_KEY_REGISTRY:
        return Response(
            {"detail": f"Unknown sensor_key '{sensor_key}'."},
            status=400,
        )
    payload = suggest_alert(request.auth, sensor_key=sensor_key, zone_id=zone_id)
    if payload is None:
        return Response({"detail": "Unable to build suggestion."}, status=404)
    return payload


# ---------------------------------------------------------------------------
# /alerts/<pk>
# ---------------------------------------------------------------------------


def _get_owned(request, pk: int) -> Alert | None:
    try:
        return Alert.objects.get(pk=pk, user=request.auth)
    except Alert.DoesNotExist:
        return None


@router.get("/{pk}", auth=JwtAuth(), summary="Retrieve one of the caller's alerts")
def get_alert(request, pk: int):
    alert = _get_owned(request, pk)
    if alert is None:
        return Response({"detail": "Not found."}, status=404)
    return _serialize(alert)


@router.put("/{pk}", auth=JwtAuth(), summary="Replace one of the caller's alerts")
def replace_alert(request, pk: int, payload: AlertWriteIn):
    blocked = block_if_technician(request)
    if blocked is not None:
        return blocked
    alert = _get_owned(request, pk)
    if alert is None:
        return Response({"detail": "Not found."}, status=404)
    err = _validate_write(request, payload)
    if err is not None:
        return err
    _apply(alert, payload)
    alert.save()
    return _serialize(alert)


@router.patch("/{pk}", auth=JwtAuth(), summary="Patch one of the caller's alerts")
def patch_alert(request, pk: int, payload: AlertWriteIn):
    blocked = block_if_technician(request)
    if blocked is not None:
        return blocked
    alert = _get_owned(request, pk)
    if alert is None:
        return Response({"detail": "Not found."}, status=404)
    err = _validate_write(request, payload)
    if err is not None:
        return err
    _apply(alert, payload)
    alert.save()
    return _serialize(alert)


@router.delete("/{pk}", auth=JwtAuth(), summary="Delete one of the caller's alerts")
def delete_alert(request, pk: int):
    blocked = block_if_technician(request)
    if blocked is not None:
        return blocked
    alert = _get_owned(request, pk)
    if alert is None:
        return Response({"detail": "Not found."}, status=404)
    alert.delete()
    return Response(None, status=204)
