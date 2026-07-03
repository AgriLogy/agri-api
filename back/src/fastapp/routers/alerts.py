"""fastapp /alerts — alert CRUD + chart overlays + suggestion prefill.

Strangler port of ``apps/alerts/router_alerts.py`` (django-ninja). Byte-parity
with the Django version:

* ``GET  /alerts`` — the caller's alerts (``-id`` order), optional
  ``sensor_key`` / ``zone_id`` filters.
* ``POST /alerts`` — create (technician-blocked, same validation).
* ``GET  /alerts/for-graph`` — active alerts annotated for chart overlays.
* ``GET  /alerts/suggest`` — mean/percentile/sd prefill for the create form.
* ``GET/PUT/PATCH/DELETE /alerts/{pk}`` — owner-scoped single-alert ops.

The serialize shape mirrors the Django ``AlertSerializer`` / ``model_to_dict``
output *exactly* (field order, the omitted ``id``, ``condition_nbr`` rendered
as its Decimal string, computed ``threshold``). Reads/writes go through the
agri-core SQLAlchemy session (no Django ORM); the fan-out + suggestion helpers
already live in ``agri.core.alerts`` and are called directly.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from agri.core.alerts import (
    SENSOR_KEY_REGISTRY,
    SUGGEST_STRATEGIES,
    recent_triggers_for_user,
    suggest_alert_for,
)
from agri.core.database import session_scope
from agri.db.analytics import AnalyticsAlert, AnalyticsNotificationzone, AnalyticsZone
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["alerts"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AlertWriteIn(BaseModel):
    """Request body for alert create/update (PUT/PATCH).

    All fields optional so PATCH semantics work; POST validation falls back to
    name + condition + condition_nbr required.
    """

    name: str | None = None
    type: str | None = None
    description: str | None = None
    condition: str | None = None
    condition_nbr: float | None = None
    sensor_key: str | None = None
    zone: int | None = None
    notification_zone: int | None = None
    is_active: bool | None = None
    notify_email: bool | None = None
    notify_whatsapp: bool | None = None
    notify_sms: bool | None = None
    grace_override_seconds: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(alert: AnalyticsAlert) -> dict[str, Any]:
    """Match ``AlertSerializer`` shape (``model_to_dict`` field order + the
    computed threshold)."""
    return {
        "id": alert.id,
        "name": alert.name,
        "type": alert.type,
        "description": alert.description,
        "condition": alert.condition,
        "condition_nbr": alert.condition_nbr,
        "sensor_key": alert.sensor_key,
        "zone": alert.zone_id,
        "is_active": alert.is_active,
        "notify_email": alert.notify_email,
        "notify_whatsapp": alert.notify_whatsapp,
        "notify_sms": alert.notify_sms,
        "notification_zone": alert.notification_zone_id,
        "grace_override_seconds": alert.grace_override_seconds,
        "threshold": (
            float(alert.condition_nbr) if alert.condition_nbr is not None else None
        ),
        "last_triggered_at": (
            alert.last_triggered_at.isoformat() if alert.last_triggered_at else None
        ),
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "updated_at": alert.updated_at.isoformat() if alert.updated_at else None,
        "user": alert.user_id,
    }


def _block_if_technician(user: AuthedUser) -> None:
    """Raise 403 when the caller is a technician (read-only). Else no-op."""
    if user.is_technician:
        raise HTTPException(status_code=403, detail="Read-only (technician) access.")


def _canonical_type_for(sensor_key: str | None) -> str | None:
    if not sensor_key:
        return None
    spec = SENSOR_KEY_REGISTRY.get(sensor_key)
    return spec.get("type") if spec else None


def _apply(alert: AnalyticsAlert, payload: AlertWriteIn) -> None:
    data = payload.model_dump(exclude_unset=True)
    if "zone" in data:
        alert.zone_id = data.pop("zone")
    if "notification_zone" in data:
        alert.notification_zone_id = data.pop("notification_zone")
    for k, v in data.items():
        setattr(alert, k, v)
    # `type` is server-authoritative when a known sensor_key is present.
    canonical = _canonical_type_for(alert.sensor_key)
    if canonical is not None:
        alert.type = canonical


def _validate_write(session, user_id: int, payload: AlertWriteIn):
    """Reject a write referencing an unknown sensor_key or an unowned zone.
    Returns a 400 ``DjangoStyleJSONResponse`` on failure, else ``None``."""
    if payload.sensor_key is not None and payload.sensor_key not in SENSOR_KEY_REGISTRY:
        return DjangoStyleJSONResponse(
            {"detail": f"Unknown sensor_key '{payload.sensor_key}'."}, status_code=400
        )
    if payload.zone is not None and not _owns(
        session, AnalyticsZone, payload.zone, user_id
    ):
        return DjangoStyleJSONResponse(
            {"detail": "zone not found or not owned by the caller."}, status_code=400
        )
    if payload.zone is not None and payload.notification_zone is not None:
        return DjangoStyleJSONResponse(
            {
                "detail": "An alert binds to either a zone or a notification_zone, not both."
            },
            status_code=400,
        )
    if payload.notification_zone is not None and not _owns(
        session, AnalyticsNotificationzone, payload.notification_zone, user_id
    ):
        return DjangoStyleJSONResponse(
            {"detail": "notification_zone not found or not owned by the caller."},
            status_code=400,
        )
    return None


def _owns(session, model, obj_id: int, user_id: int) -> bool:
    return (
        session.scalars(
            select(model.id)
            .where(model.id == obj_id, model.user_id == user_id)
            .limit(1)
        ).first()
        is not None
    )


def _get_owned(session, pk: int, user_id: int) -> AnalyticsAlert | None:
    return session.scalars(
        select(AnalyticsAlert).where(
            AnalyticsAlert.id == pk, AnalyticsAlert.user_id == user_id
        )
    ).first()


# ---------------------------------------------------------------------------
# /alerts
# ---------------------------------------------------------------------------


@router.get("/alerts", summary="List the caller's alerts")
def list_alerts(
    sensor_key: str | None = None,
    zone_id: int | None = None,
    user: AuthedUser = Depends(get_current_user),
):
    criteria = [AnalyticsAlert.user_id == user.id]
    if sensor_key:
        criteria.append(AnalyticsAlert.sensor_key == sensor_key)
    if zone_id is not None:
        criteria.append(AnalyticsAlert.zone_id == zone_id)
    with session_scope() as session:
        rows = session.scalars(
            select(AnalyticsAlert).where(*criteria).order_by(AnalyticsAlert.id.desc())
        ).all()
        # Return the response directly (not a plain list) so FastAPI's
        # jsonable_encoder can't coerce the Decimal ``condition_nbr`` to a float
        # — ninja renders it as its "30.00" string and we must match.
        return DjangoStyleJSONResponse([_serialize(a) for a in rows])


@router.post("/alerts", summary="Create an alert for the caller")
def create_alert(
    payload: AlertWriteIn,
    user: AuthedUser = Depends(get_current_user),
):
    _block_if_technician(user)
    if not payload.name or not payload.condition or payload.condition_nbr is None:
        return DjangoStyleJSONResponse(
            {"detail": "name, condition, and condition_nbr are required."},
            status_code=400,
        )
    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope(commit=True) as session:
        err = _validate_write(session, user.id, payload)
        if err is not None:
            return err
        alert = AnalyticsAlert(
            user_id=user.id,
            name="",
            type="",
            description="",
            condition="",
            condition_nbr=0,
            sensor_key="",
            is_active=True,
            notify_email=True,
            notify_whatsapp=False,
            notify_sms=False,
            grace_override_seconds=None,
            created_at=now,
            updated_at=now,
        )
        _apply(alert, payload)
        session.add(alert)
        session.flush()
        return DjangoStyleJSONResponse(_serialize(alert), status_code=201)


# --- Sub-collections MUST precede /{pk} so the dynamic path doesn't shadow --


@router.get("/alerts/for-graph", summary="Alerts annotated for chart overlays")
def alerts_for_graph(
    sensor_key: str | None = None,
    zone_id: int | None = None,
    user: AuthedUser = Depends(get_current_user),
):
    if sensor_key and sensor_key not in SENSOR_KEY_REGISTRY:
        return DjangoStyleJSONResponse(
            {"detail": f"Unknown sensor_key '{sensor_key}'."}, status_code=400
        )
    with session_scope(commit=True) as session:
        rows = recent_triggers_for_user(
            session,
            user.id,
            sensor_key=sensor_key,
            zone_id=zone_id,
            now=datetime.datetime.now(datetime.timezone.utc),
        )
    return {"alerts": rows}


@router.get(
    "/alerts/suggest",
    summary="Suggested alert payload (mean / percentile / sd prefill)",
)
def alerts_suggest(
    sensor_key: str | None = None,
    zone_id: int | None = None,
    strategy: str = "mean",
    user: AuthedUser = Depends(get_current_user),
):
    if not sensor_key:
        return DjangoStyleJSONResponse(
            {"detail": "sensor_key is required."}, status_code=400
        )
    if sensor_key not in SENSOR_KEY_REGISTRY:
        return DjangoStyleJSONResponse(
            {"detail": f"Unknown sensor_key '{sensor_key}'."}, status_code=400
        )
    if strategy not in SUGGEST_STRATEGIES:
        return DjangoStyleJSONResponse(
            {
                "detail": f"Unknown strategy '{strategy}'. Allowed: {list(SUGGEST_STRATEGIES)}."
            },
            status_code=400,
        )
    with session_scope() as session:
        payload = suggest_alert_for(
            session, user.id, sensor_key, zone_id=zone_id, limit=50, strategy=strategy
        )
    if payload is None:
        return DjangoStyleJSONResponse(
            {"detail": "Unable to build suggestion."}, status_code=404
        )
    return payload


# ---------------------------------------------------------------------------
# /alerts/{pk}
# ---------------------------------------------------------------------------


@router.get("/alerts/{pk}", summary="Retrieve one of the caller's alerts")
def get_alert(pk: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        alert = _get_owned(session, pk, user.id)
        if alert is None:
            return DjangoStyleJSONResponse({"detail": "Not found."}, status_code=404)
        return DjangoStyleJSONResponse(_serialize(alert))


@router.put("/alerts/{pk}", summary="Replace one of the caller's alerts")
def replace_alert(
    pk: int, payload: AlertWriteIn, user: AuthedUser = Depends(get_current_user)
):
    return _write_existing(pk, payload, user)


@router.patch("/alerts/{pk}", summary="Patch one of the caller's alerts")
def patch_alert(
    pk: int, payload: AlertWriteIn, user: AuthedUser = Depends(get_current_user)
):
    return _write_existing(pk, payload, user)


def _write_existing(pk: int, payload: AlertWriteIn, user: AuthedUser):
    _block_if_technician(user)
    with session_scope(commit=True) as session:
        alert = _get_owned(session, pk, user.id)
        if alert is None:
            return DjangoStyleJSONResponse({"detail": "Not found."}, status_code=404)
        err = _validate_write(session, user.id, payload)
        if err is not None:
            return err
        _apply(alert, payload)
        alert.updated_at = datetime.datetime.now(datetime.timezone.utc)
        session.flush()
        return DjangoStyleJSONResponse(_serialize(alert))


@router.delete("/alerts/{pk}", summary="Delete one of the caller's alerts")
def delete_alert(pk: int, user: AuthedUser = Depends(get_current_user)):
    _block_if_technician(user)
    with session_scope(commit=True) as session:
        alert = _get_owned(session, pk, user.id)
        if alert is None:
            return DjangoStyleJSONResponse({"detail": "Not found."}, status_code=404)
        session.delete(alert)
    return DjangoStyleJSONResponse(None, status_code=204)
