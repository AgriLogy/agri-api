"""fastapp /notification-zones — user-owned alert groupings (agrilogy-front #57).

Strangler port of ``apps/alerts/router_notification_zones.py`` (django-ninja).
Byte-parity with the Django version across all routes:

  * GET, POST            /notification-zones
  * GET                  /notification-zones/available-sensors
  * GET, PATCH, DELETE   /notification-zones/{pk}
  * POST                 /notification-zones/{pk}/sensors
  * DELETE               /notification-zones/{pk}/sensors/{sensor_id}

All endpoints are owner-scoped (JWT ``user.id``); a technician is blocked from
writes (403) like the Django router. Data access is SQLAlchemy via agri-core
(no Django ORM). Timestamps are set explicitly on write to mirror the Django
model's ``auto_now_add`` (create) / ``auto_now`` (create + save) columns.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agri.core.alerts import SENSOR_KEY_REGISTRY, db_model_for
from agri.core.database import session_scope
from agri.db.analytics import (
    AnalyticsNotificationzone,
    AnalyticsNotificationzonesensor,
    AnalyticsZone,
)
from agri.db.users import CustomUserCustomuser
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["notification-zones"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SensorAssignmentIn(BaseModel):
    sensor_key: str
    source_zone: int | None = None
    label: str | None = None


class NotificationZoneIn(BaseModel):
    """Create/update body. All optional so PATCH works; ``sensors``, when
    provided, REPLACES the zone's assignments wholesale."""

    name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    sensors: list[SensorAssignmentIn] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _serialize_sensor(s: AnalyticsNotificationzonesensor) -> dict[str, Any]:
    spec = SENSOR_KEY_REGISTRY.get(s.sensor_key, {})
    return {
        "id": s.id,
        "sensor_key": s.sensor_key,
        "source_zone": s.source_zone_id,
        "label": s.label or spec.get("label"),
        "unit": spec.get("unit"),
    }


def _zone_sensors(
    session: Session, nz_id: int
) -> list[AnalyticsNotificationzonesensor]:
    return list(
        session.scalars(
            select(AnalyticsNotificationzonesensor)
            .where(AnalyticsNotificationzonesensor.notification_zone_id == nz_id)
            .order_by(AnalyticsNotificationzonesensor.id)
        ).all()
    )


def _serialize(session: Session, nz: AnalyticsNotificationzone) -> dict[str, Any]:
    return {
        "id": nz.id,
        "name": nz.name,
        "description": nz.description,
        "is_active": nz.is_active,
        "user": nz.user_id,
        "created_at": nz.created_at.isoformat() if nz.created_at else None,
        "updated_at": nz.updated_at.isoformat() if nz.updated_at else None,
        "sensors": [_serialize_sensor(s) for s in _zone_sensors(session, nz.id)],
    }


def _owned(session: Session, pk: int, user_id: int) -> AnalyticsNotificationzone | None:
    return session.scalars(
        select(AnalyticsNotificationzone)
        .where(
            AnalyticsNotificationzone.id == pk,
            AnalyticsNotificationzone.user_id == user_id,
        )
        .order_by(AnalyticsNotificationzone.id)
        .limit(1)
    ).first()


def _block_if_technician(session: Session, user_id: int) -> None:
    """Raise 403 when the caller is a technician (read-only), matching the
    Django ``block_if_technician`` response byte-for-byte."""
    row = session.get(CustomUserCustomuser, user_id)
    if row is not None and getattr(row, "is_technician", False):
        raise HTTPException(status_code=403, detail="Read-only (technician) access.")


def _validate_assignment(
    session: Session, user_id: int, a: SensorAssignmentIn
) -> str | None:
    if a.sensor_key not in SENSOR_KEY_REGISTRY:
        return f"Unknown sensor_key '{a.sensor_key}'."
    if a.source_zone is not None:
        owns_zone = session.scalars(
            select(AnalyticsZone.id)
            .where(
                AnalyticsZone.id == a.source_zone,
                AnalyticsZone.user_id == user_id,
            )
            .limit(1)
        ).first()
        if owns_zone is None:
            return f"source_zone {a.source_zone} not found or not owned by the caller."
    return None


def _replace_sensors(
    session: Session, nz_id: int, assignments: list[SensorAssignmentIn]
) -> None:
    session.execute(
        delete(AnalyticsNotificationzonesensor).where(
            AnalyticsNotificationzonesensor.notification_zone_id == nz_id
        )
    )
    for a in assignments:
        session.add(
            AnalyticsNotificationzonesensor(
                notification_zone_id=nz_id,
                sensor_key=a.sensor_key,
                source_zone_id=a.source_zone,
                label=a.label,
            )
        )
    session.flush()


# ---------------------------------------------------------------------------
# /notification-zones
# ---------------------------------------------------------------------------


@router.get(
    "/notification-zones",
    summary="List the caller's notification zones",
)
def list_zones(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        zones = session.scalars(
            select(AnalyticsNotificationzone)
            .where(AnalyticsNotificationzone.user_id == user.id)
            .order_by(AnalyticsNotificationzone.id.desc())
        ).all()
        return [_serialize(session, nz) for nz in zones]


@router.post(
    "/notification-zones",
    summary="Create a notification zone",
)
def create_zone(
    payload: NotificationZoneIn,
    user: AuthedUser = Depends(get_current_user),
):
    now = _now()
    with session_scope(commit=True) as session:
        _block_if_technician(session, user.id)
        if not payload.name:
            raise HTTPException(status_code=400, detail="name is required.")
        for a in payload.sensors or []:
            err = _validate_assignment(session, user.id, a)
            if err is not None:
                raise HTTPException(status_code=400, detail=err)
        nz = AnalyticsNotificationzone(
            user_id=user.id,
            name=payload.name,
            description=payload.description or "",
            is_active=payload.is_active if payload.is_active is not None else True,
            created_at=now,
            updated_at=now,
        )
        session.add(nz)
        session.flush()
        if payload.sensors is not None:
            _replace_sensors(session, nz.id, payload.sensors)
        result = _serialize(session, nz)
    return DjangoStyleJSONResponse(result, status_code=201)


# Sub-collections MUST be registered before /{pk} so the dynamic path doesn't
# shadow them (a non-int segment would 422 on /{pk} rather than fall through).
@router.get(
    "/notification-zones/available-sensors",
    summary="The caller's (farm zone, sensor_key) reading streams",
)
def available_sensors(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        zones = session.execute(
            select(AnalyticsZone.id, AnalyticsZone.name).where(
                AnalyticsZone.user_id == user.id
            )
        ).all()
        out = []
        for z in zones:
            zone_id, zone_name = z[0], z[1]
            keys = []
            for key, spec in SENSOR_KEY_REGISTRY.items():
                try:
                    model = db_model_for(key)
                    exists_row = session.scalars(
                        select(model.id)
                        .where(model.user_id == user.id, model.zone_id == zone_id)
                        .limit(1)
                    ).first()
                    if exists_row is not None:
                        keys.append(
                            {
                                "sensor_key": key,
                                "label": spec.get("label"),
                                "unit": spec.get("unit"),
                            }
                        )
                except Exception:
                    # A model-resolution / query error for one sensor key must
                    # not sink the whole listing, but log it so it's diagnosable
                    # rather than silently dropped.
                    log.warning(
                        "available_sensors: skipping sensor_key %s for zone %s",
                        key,
                        zone_id,
                        exc_info=True,
                    )
                    continue
            out.append({"zone_id": zone_id, "zone_name": zone_name, "sensors": keys})
        return {"zones": out}


@router.get(
    "/notification-zones/{pk}",
    summary="Get one notification zone",
)
def get_zone(pk: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        nz = _owned(session, pk, user.id)
        if nz is None:
            raise HTTPException(status_code=404, detail="Notification zone not found.")
        return _serialize(session, nz)


@router.patch(
    "/notification-zones/{pk}",
    summary="Update a notification zone",
)
def update_zone(
    pk: int,
    payload: NotificationZoneIn,
    user: AuthedUser = Depends(get_current_user),
):
    now = _now()
    with session_scope(commit=True) as session:
        _block_if_technician(session, user.id)
        nz = _owned(session, pk, user.id)
        if nz is None:
            raise HTTPException(status_code=404, detail="Notification zone not found.")
        for a in payload.sensors or []:
            err = _validate_assignment(session, user.id, a)
            if err is not None:
                raise HTTPException(status_code=400, detail=err)
        data = payload.model_dump(exclude_unset=True)
        for field in ("name", "description", "is_active"):
            if field in data and data[field] is not None:
                setattr(nz, field, data[field])
        nz.updated_at = now  # mirror the model's auto_now on save()
        session.flush()
        if payload.sensors is not None:
            _replace_sensors(session, nz.id, payload.sensors)
        result = _serialize(session, nz)
    return result


@router.delete(
    "/notification-zones/{pk}",
    summary="Delete a notification zone",
)
def delete_zone(pk: int, user: AuthedUser = Depends(get_current_user)):
    with session_scope(commit=True) as session:
        _block_if_technician(session, user.id)
        nz = _owned(session, pk, user.id)
        if nz is None:
            raise HTTPException(status_code=404, detail="Notification zone not found.")
        session.execute(
            delete(AnalyticsNotificationzonesensor).where(
                AnalyticsNotificationzonesensor.notification_zone_id == nz.id
            )
        )
        session.delete(nz)
        session.flush()
    return DjangoStyleJSONResponse({"deleted": pk}, status_code=200)


@router.post(
    "/notification-zones/{pk}/sensors",
    summary="Add a sensor assignment",
)
def add_sensor(
    pk: int,
    payload: SensorAssignmentIn,
    user: AuthedUser = Depends(get_current_user),
):
    with session_scope(commit=True) as session:
        _block_if_technician(session, user.id)
        nz = _owned(session, pk, user.id)
        if nz is None:
            raise HTTPException(status_code=404, detail="Notification zone not found.")
        err = _validate_assignment(session, user.id, payload)
        if err is not None:
            raise HTTPException(status_code=400, detail=err)
        s = AnalyticsNotificationzonesensor(
            notification_zone_id=nz.id,
            sensor_key=payload.sensor_key,
            source_zone_id=payload.source_zone,
            label=payload.label,
        )
        session.add(s)
        session.flush()
        result = _serialize_sensor(s)
    return DjangoStyleJSONResponse(result, status_code=201)


@router.delete(
    "/notification-zones/{pk}/sensors/{sensor_id}",
    summary="Remove a sensor assignment",
)
def remove_sensor(
    pk: int,
    sensor_id: int,
    user: AuthedUser = Depends(get_current_user),
):
    with session_scope(commit=True) as session:
        _block_if_technician(session, user.id)
        nz = _owned(session, pk, user.id)
        if nz is None:
            raise HTTPException(status_code=404, detail="Notification zone not found.")
        deleted = session.execute(
            delete(AnalyticsNotificationzonesensor).where(
                AnalyticsNotificationzonesensor.id == sensor_id,
                AnalyticsNotificationzonesensor.notification_zone_id == nz.id,
            )
        ).rowcount
        if not deleted:
            raise HTTPException(status_code=404, detail="Sensor assignment not found.")
        session.flush()
    return DjangoStyleJSONResponse({"deleted": sensor_id}, status_code=200)
