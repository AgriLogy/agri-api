"""fastapp /sensor-groups — the farmer's own groupings of individual sensors.

A sensor is the pair ``(device_id, sensor_key)``; a group is a named bucket of
such pairs, owned by one account. The same sensor may belong to any number of
groups (the membership table's uniqueness is per group).

Storage (agri-db migration ``f4b6d2e8c1a9``):

* ``analytics_sensorgroup``        — the group (owner, name, order, active);
* ``analytics_sensorgroupsensor``  — membership rows, ``ON DELETE CASCADE``
  from the group, so deleting a group removes only its memberships.

Two constraints shape this module:

1. **Raw SQL.** agri-api pins agri-core 0.23.0 → agri-db 0.17.0, which predates
   these tables, so there are no ORM models to import. Statements are
   parameterised ``text()``, the same idiom as ``routers/devices.py``.
2. **The migration is not applied in production.** Every entry point asks
   ``fastapp.schema_compat.sensor_groups_available`` first: reads answer empty,
   writes are refused with a 400 naming the migration. Never an
   ``UndefinedTable`` 500.

Scoping reuses ``routers/selfreads._resolve_read_scope``: regular users see and
edit their own groups; technicians are read-only (writes → 403) and only see
memberships whose sensor sits in a zone granted to them.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text

from agri.core.database import session_scope
from agri.db.users import CustomUserCustomuser
from fastapp.auth import AuthedUser, get_current_user, require_level
from fastapp.json import DjangoStyleJSONResponse
from fastapp.routers.selfreads import _ReadScope, _resolve_read_scope
from fastapp.schema_compat import SENSOR_GROUPS_UNAVAILABLE, sensor_groups_available

router = APIRouter(tags=["sensor-groups"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SensorGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    display_order: int | None = None
    is_active: bool | None = None


class SensorGroupPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    display_order: int | None = None
    is_active: bool | None = None


class GroupSensorIn(BaseModel):
    device_id: int
    sensor_key: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=120)
    zone_id: int | None = None
    display_order: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _unavailable() -> DjangoStyleJSONResponse:
    """The write refusal used everywhere the tables are missing (400, naming
    the migration) — mirrors the coordinate shim's rejection."""
    return DjangoStyleJSONResponse(
        {"detail": SENSOR_GROUPS_UNAVAILABLE}, status_code=400
    )


def _scope(session, user_id: int) -> _ReadScope:
    row = session.get(CustomUserCustomuser, user_id)
    if row is None:  # pragma: no cover - a JWT for a deleted user
        return _ReadScope(owner_id=user_id, zone_ids=None)
    return _resolve_read_scope(session, row)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize_group(row, sensors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "display_order": row.display_order,
        "is_active": row.is_active,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "sensors": sensors,
    }


def _serialize_sensor(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "group_id": row.group_id,
        "device_id": row.device_id,
        "sensor_key": row.sensor_key,
        "label": row.label,
        "zone_id": row.zone_id,
        "display_order": row.display_order,
        "device_name": row.device_name,
        "device_serial": row.device_serial,
    }


_GROUP_COLUMNS = (
    "id, name, description, display_order, is_active, created_at, updated_at"
)

_SENSOR_SELECT = (
    "SELECT s.id, s.group_id, s.device_id, s.sensor_key, s.label, s.zone_id, "
    "s.display_order, d.name AS device_name, d.serial AS device_serial, "
    "COALESCE(s.zone_id, d.zone_id) AS effective_zone_id "
    "FROM analytics_sensorgroupsensor s "
    "LEFT JOIN analytics_device d ON d.id = s.device_id "
)


def _sensors_for(session, group_ids: list[int], scope: _ReadScope) -> dict[int, list]:
    """Membership rows per group, filtered to the zones the caller may read."""
    if not group_ids:
        return {}
    stmt = text(
        _SENSOR_SELECT + "WHERE s.group_id IN :gids ORDER BY s.display_order, s.id"
    ).bindparams(bindparam("gids", expanding=True))
    out: dict[int, list] = {}
    for row in session.execute(stmt, {"gids": group_ids}):
        if not scope.zone_allowed(row.effective_zone_id):
            continue
        out.setdefault(row.group_id, []).append(_serialize_sensor(row))
    return out


def _owned_group(session, pk: int, owner_id: int):
    return session.execute(
        text(
            f"SELECT {_GROUP_COLUMNS} FROM analytics_sensorgroup "
            "WHERE id = :pk AND user_id = :owner"
        ),
        {"pk": pk, "owner": owner_id},
    ).first()


def _not_found() -> DjangoStyleJSONResponse:
    return DjangoStyleJSONResponse(
        {"detail": "Sensor group not found."}, status_code=404
    )


def _block_if_read_only(scope: _ReadScope) -> DjangoStyleJSONResponse | None:
    if scope.is_read_only:
        return DjangoStyleJSONResponse(
            {"detail": "Read-only (technician) access."}, status_code=403
        )
    return None


def _name_taken(session, owner_id: int, name: str, exclude_id: int | None) -> bool:
    sql = "SELECT 1 FROM analytics_sensorgroup WHERE user_id = :owner AND name = :name"
    params: dict[str, Any] = {"owner": owner_id, "name": name}
    if exclude_id is not None:
        sql += " AND id <> :pk"
        params["pk"] = exclude_id
    return session.execute(text(sql), params).first() is not None


# ---------------------------------------------------------------------------
# Group CRUD
# ---------------------------------------------------------------------------
@router.get("/sensor-groups", summary="Caller's sensor groups, with their sensors")
def list_sensor_groups(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        # COMPAT SHIM (#439) — no tables yet means the farmer simply has no
        # groups; an empty list is the honest answer, never a 500.
        if not sensor_groups_available(session):
            return []
        scope = _scope(session, user.id)
        rows = session.execute(
            text(
                f"SELECT {_GROUP_COLUMNS} FROM analytics_sensorgroup "
                "WHERE user_id = :owner ORDER BY display_order, name"
            ),
            {"owner": scope.owner_id},
        ).all()
        sensors = _sensors_for(session, [r.id for r in rows], scope)
        return [_serialize_group(r, sensors.get(r.id, [])) for r in rows]


@router.post("/sensor-groups", summary="Create a sensor group")
def create_sensor_group(
    payload: SensorGroupIn,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    with session_scope(commit=True) as session:
        if not sensor_groups_available(session):
            return _unavailable()
        scope = _scope(session, user.id)
        blocked = _block_if_read_only(scope)
        if blocked is not None:
            return blocked
        name = payload.name.strip()
        if not name:
            return DjangoStyleJSONResponse(
                {"detail": "name is required."}, status_code=400
            )
        # UNIQUE (user_id, name) — answer with a clear 400 instead of letting
        # the constraint surface as a 500.
        if _name_taken(session, scope.owner_id, name, None):
            return DjangoStyleJSONResponse(
                {"detail": "A group with that name already exists."}, status_code=400
            )
        now = _now()
        new_id = session.execute(
            text(
                "INSERT INTO analytics_sensorgroup "
                "(user_id, name, description, display_order, is_active, "
                " created_at, updated_at) "
                "VALUES (:owner, :name, :description, :display_order, :is_active, "
                " :now, :now) RETURNING id"
            ),
            {
                "owner": scope.owner_id,
                "name": name,
                "description": payload.description or "",
                "display_order": payload.display_order or 0,
                "is_active": True if payload.is_active is None else payload.is_active,
                "now": now,
            },
        ).scalar_one()
        row = _owned_group(session, new_id, scope.owner_id)
        return DjangoStyleJSONResponse(_serialize_group(row, []), status_code=201)


@router.patch("/sensor-groups/{pk}", summary="Update a sensor group")
def update_sensor_group(
    pk: int,
    payload: SensorGroupPatchIn,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    with session_scope(commit=True) as session:
        if not sensor_groups_available(session):
            return _unavailable()
        scope = _scope(session, user.id)
        blocked = _block_if_read_only(scope)
        if blocked is not None:
            return blocked
        if _owned_group(session, pk, scope.owner_id) is None:
            return _not_found()
        updates: dict[str, Any] = {}
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                return DjangoStyleJSONResponse(
                    {"detail": "name is required."}, status_code=400
                )
            if _name_taken(session, scope.owner_id, name, pk):
                return DjangoStyleJSONResponse(
                    {"detail": "A group with that name already exists."},
                    status_code=400,
                )
            updates["name"] = name
        if payload.description is not None:
            updates["description"] = payload.description
        if payload.display_order is not None:
            updates["display_order"] = payload.display_order
        if payload.is_active is not None:
            updates["is_active"] = payload.is_active
        if updates:
            updates["updated_at"] = _now()
            set_clause = ", ".join(f"{col} = :{col}" for col in updates)
            session.execute(
                text(
                    f"UPDATE analytics_sensorgroup SET {set_clause} "
                    "WHERE id = :pk AND user_id = :owner"
                ),
                {**updates, "pk": pk, "owner": scope.owner_id},
            )
        row = _owned_group(session, pk, scope.owner_id)
        sensors = _sensors_for(session, [pk], scope)
        return _serialize_group(row, sensors.get(pk, []))


@router.delete("/sensor-groups/{pk}", summary="Delete a sensor group")
def delete_sensor_group(
    pk: int,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("admin")),
):
    with session_scope(commit=True) as session:
        if not sensor_groups_available(session):
            return _unavailable()
        scope = _scope(session, user.id)
        blocked = _block_if_read_only(scope)
        if blocked is not None:
            return blocked
        # Membership rows go with it (FK ON DELETE CASCADE); the sensors and
        # their devices are untouched.
        deleted = session.execute(
            text(
                "DELETE FROM analytics_sensorgroup WHERE id = :pk AND user_id = :owner "
                "RETURNING id"
            ),
            {"pk": pk, "owner": scope.owner_id},
        ).first()
        if deleted is None:
            return _not_found()
        return DjangoStyleJSONResponse({"success": True}, status_code=200)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------
@router.post("/sensor-groups/{pk}/sensors", summary="Add a sensor to a group")
def add_sensor_to_group(
    pk: int,
    payload: GroupSensorIn,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("editor")),
):
    with session_scope(commit=True) as session:
        if not sensor_groups_available(session):
            return _unavailable()
        scope = _scope(session, user.id)
        blocked = _block_if_read_only(scope)
        if blocked is not None:
            return blocked
        if _owned_group(session, pk, scope.owner_id) is None:
            return _not_found()
        # The sensor's device must belong to the same account — otherwise a
        # farmer could pull a neighbour's readings into their own dashboard.
        device = session.execute(
            text(
                "SELECT id, zone_id FROM analytics_device "
                "WHERE id = :device AND user_id = :owner"
            ),
            {"device": payload.device_id, "owner": scope.owner_id},
        ).first()
        if device is None:
            return DjangoStyleJSONResponse(
                {"detail": "Device not found."}, status_code=404
            )
        zone_id = payload.zone_id
        if zone_id is not None:
            zone = session.execute(
                text(
                    "SELECT id FROM analytics_zone WHERE id = :zone AND user_id = :owner"
                ),
                {"zone": zone_id, "owner": scope.owner_id},
            ).first()
            if zone is None:
                return DjangoStyleJSONResponse(
                    {"detail": "Zone not found."}, status_code=404
                )
        else:
            zone_id = device.zone_id
        sensor_key = payload.sensor_key.strip()
        # UNIQUE (group_id, device_id, sensor_key): the same sensor may sit in
        # many groups, but only once per group.
        exists = session.execute(
            text(
                "SELECT 1 FROM analytics_sensorgroupsensor "
                "WHERE group_id = :gid AND device_id = :device "
                "AND sensor_key = :key LIMIT 1"
            ),
            {"gid": pk, "device": payload.device_id, "key": sensor_key},
        ).first()
        if exists is not None:
            return DjangoStyleJSONResponse(
                {"detail": "That sensor is already in this group."}, status_code=400
            )
        new_id = session.execute(
            text(
                "INSERT INTO analytics_sensorgroupsensor "
                "(group_id, device_id, sensor_key, label, zone_id, display_order, "
                " created_at) "
                "VALUES (:gid, :device, :key, :label, :zone, :display_order, :now) "
                "RETURNING id"
            ),
            {
                "gid": pk,
                "device": payload.device_id,
                "key": sensor_key,
                "label": payload.label or "",
                "zone": zone_id,
                "display_order": payload.display_order or 0,
                "now": _now(),
            },
        ).scalar_one()
        row = session.execute(
            text(_SENSOR_SELECT + "WHERE s.id = :sid"), {"sid": new_id}
        ).first()
        return DjangoStyleJSONResponse(_serialize_sensor(row), status_code=201)


@router.delete(
    "/sensor-groups/{pk}/sensors/{sensor_id}",
    summary="Remove a sensor from a group",
)
def remove_sensor_from_group(
    pk: int,
    sensor_id: int,
    user: AuthedUser = Depends(get_current_user),
    _: AuthedUser = Depends(require_level("admin")),
):
    with session_scope(commit=True) as session:
        if not sensor_groups_available(session):
            return _unavailable()
        scope = _scope(session, user.id)
        blocked = _block_if_read_only(scope)
        if blocked is not None:
            return blocked
        if _owned_group(session, pk, scope.owner_id) is None:
            return _not_found()
        deleted = session.execute(
            text(
                "DELETE FROM analytics_sensorgroupsensor "
                "WHERE id = :sid AND group_id = :gid RETURNING id"
            ),
            {"sid": sensor_id, "gid": pk},
        ).first()
        if deleted is None:
            return DjangoStyleJSONResponse(
                {"detail": "Sensor not in this group."}, status_code=404
            )
        return DjangoStyleJSONResponse({"success": True}, status_code=200)
