"""fastapp admin analytics tree (staff only).

Strangler port of ``apps/irrigation/router_admin.py`` (django-ninja, the
``analytics_admin_router``): dashboard KPIs, business analytics, device-fleet
health, per-user zone/param/active-graph/graph-config CRUD, the alerts admin
console + override, per-user activity timeline, and per-user sensor-unit prefs.

All routes require JWT + ``is_staff``. Reads/writes go through the agri-core
SQLAlchemy session (no Django ORM); the unmanaged ``lora_uplink`` table is read
via raw SQL (as admin_monitoring does). Byte-parity with the Django version for
reads + 404 envelopes; timestamped writes (create/patch) are compared
structurally because ``created_at``/``updated_at`` are live.

Only the ``/admin/*`` paths of this router are cut over in nginx; the
``/users/{username}/*`` paths share the ``/users`` prefix with the (still
Django-served) users-admin router, so they stay on Django for now — the ports
here are exercised via the sidecar's TestClient.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select, text

from agri.core.database import session_scope
from agri.db.analytics import (
    AnalyticsActivegraph,
    AnalyticsAlert,
    AnalyticsGraphname,
    AnalyticsSensorcolor,
    AnalyticsUsersensorunitpreference,
    AnalyticsZone,
)
from agri.db.users import CustomUserCustomuser
from fastapp.adminutil import record_audit, username_for
from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["admin"])


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _404(detail: str):
    return DjangoStyleJSONResponse({"detail": detail}, status_code=404)


def _400(detail):
    return DjangoStyleJSONResponse(detail, status_code=400)


# ---------------------------------------------------------------------------
# Zone serialization
# ---------------------------------------------------------------------------
ZONE_FIELDS = [
    "id",
    "name",
    "space",
    "soil_param_TAW",
    "soil_param_FC",
    "soil_param_WP",
    "soil_param_RAW",
    "critical_moisture_threshold",
    "pomp_flow_rate",
    "irrigation_water_quantity",
    "elevation_m",
]

ZONE_PARAMS_FIELDS = [
    "id",
    "soil_param_TAW",
    "soil_param_FC",
    "soil_param_WP",
    "soil_param_RAW",
    "critical_moisture_threshold",
    "pomp_flow_rate",
    "irrigation_water_quantity",
]

# Django ActiveGraph model field order (model_to_dict, id popped, FK as pk).
ACTIVE_GRAPH_STATUS_FIELDS = [
    "soil_irrigation_status",
    "soil_ph_status",
    "soil_conductivity_status",
    "soil_moisture_status",
    "soil_temperature_status",
    "et0_status",
    "wind_speed_status",
    "wind_direction_status",
    "solar_radiation_status",
    "temperature_humidity_weather_status",
    "precipitation_humidity_rate_status",
    "pluviometry_status",
    "data_table_status",
    "wind_radar_status",
    "cumulative_precipitation_status",
    "precipitation_rate_status",
    "weather_temperature_humidity_status",
    "water_flow_status",
    "water_pressure_status",
    "water_ph_status",
    "water_ec_status",
    "water_level_status",
    "leaf_sensor_status",
    "fruit_size_status",
    "large_fruit_diameter_status",
    "npk_status",
    "electricity_consumption_status",
]

# Django GraphName field order (model_to_dict, id/user/zone popped).
GRAPH_NAME_FIELDS = [
    "soil_irrigation",
    "soil_ph",
    "soil_conductivity",
    "soil_moisture",
    "soil_temperature",
    "et0",
    "precipitation_rate",
    "wind_speed",
    "solar_radiation",
    "pressure_weather",
    "wind_direction",
    "humidity_weather",
    "temperature_weather",
    "temperature_humidity_weather",
    "precipitation_humidity_rate",
    "pluviometrie",
    "data_table",
]

# Django SensorColor field order (model_to_dict, id/user/zone popped).
SENSOR_COLOR_FIELDS = [
    "precipitation_rate_color",
    "humidity_weather_color",
    "wind_speed_color",
    "solar_radiation_color",
    "pressure_weather_color",
    "wind_direction_color",
    "temperature_weather_color",
    "et0_color",
    "ec_soil_medium_color",
    "soil_temperature_medium_color",
    "soil_ec_high_color",
    "ec_soil_low_color",
    "soil_moisture_medium_color",
    "soil_moisture_high_color",
    "soil_moisture_low_color",
    "ph_soil_color",
    "soil_temperature_low_color",
    "soil_temperature_high_color",
]


def _serialize_zone(z: AnalyticsZone) -> dict[str, Any]:
    return {f: getattr(z, f) for f in ZONE_FIELDS}


def _serialize_zone_params(z: AnalyticsZone) -> dict[str, Any]:
    return {f: getattr(z, f) for f in ZONE_PARAMS_FIELDS}


def _serialize_active_graph(ag: AnalyticsActivegraph) -> dict[str, Any]:
    out: dict[str, Any] = {"user": ag.user_id, "zone": ag.zone_id}
    for f in ACTIVE_GRAPH_STATUS_FIELDS:
        out[f] = getattr(ag, f)
    return out


def _serialize_config(obj, fields: list[str]) -> dict[str, Any]:
    return {f: getattr(obj, f) for f in fields}


def _validate_zone(payload: dict[str, Any]) -> dict | None:
    if "space" in payload and payload["space"] is not None and payload["space"] <= 0:
        return {"space": "Space must be strictly positive."}
    cmt = payload.get("critical_moisture_threshold")
    if cmt is not None and (cmt < 0 or cmt > 100):
        return {"critical_moisture_threshold": "Threshold must be between 0 and 100."}
    pfr = payload.get("pomp_flow_rate")
    if pfr is not None and pfr < 0:
        return {"pomp_flow_rate": "Flow rate must be non-negative."}
    fc = payload.get("soil_param_FC")
    wp = payload.get("soil_param_WP")
    if fc is not None and wp is not None and fc < wp:
        return {
            "non_field_errors": (
                "Field capacity (FC) cannot be lower than wilting point (WP)."
            )
        }
    return None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ZoneWriteIn(BaseModel):
    name: str | None = None
    space: float | None = None
    soil_param_TAW: float | None = None
    soil_param_FC: float | None = None
    soil_param_WP: float | None = None
    soil_param_RAW: float | None = None
    critical_moisture_threshold: float | None = None
    pomp_flow_rate: float | None = None
    irrigation_water_quantity: float | None = None
    elevation_m: float | None = None


class AlertAdminPatchIn(BaseModel):
    is_active: bool | None = None


class AlertAdminCreateIn(BaseModel):
    username: str
    name: str
    type: str
    condition: str
    condition_nbr: float
    sensor_key: str = ""
    description: str = ""
    zone_id: int | None = None
    is_active: bool = True


def _resolve_user(session, username: str) -> CustomUserCustomuser | None:
    return session.scalars(
        select(CustomUserCustomuser).where(CustomUserCustomuser.username == username)
    ).first()


# ---------------------------------------------------------------------------
# Overview + business analytics
# ---------------------------------------------------------------------------
@router.get("/admin/overview", summary="Admin: dashboard KPIs")
def overview(user: AuthedUser = Depends(get_current_staff_user)):
    since = _utcnow() - datetime.timedelta(hours=24)
    with session_scope() as session:

        def _count(model, *crit):
            return session.scalar(select(func.count()).select_from(model).where(*crit))

        return {
            "users_total": _count(CustomUserCustomuser),
            "users_active": _count(
                CustomUserCustomuser, CustomUserCustomuser.is_active.is_(True)
            ),
            "staff_total": _count(
                CustomUserCustomuser, CustomUserCustomuser.is_staff.is_(True)
            ),
            "zones_total": _count(AnalyticsZone),
            "alerts_24h": _count(
                AnalyticsAlert, AnalyticsAlert.last_triggered_at >= since
            ),
        }


@router.get("/admin/analytics", summary="Admin: business analytics")
def analytics(user: AuthedUser = Depends(get_current_staff_user)):
    now = _utcnow()
    with session_scope() as session:
        customer_filter = (
            CustomUserCustomuser.is_staff.is_(False),
            CustomUserCustomuser.is_technician.is_(False),
        )

        # Payment status split.
        payment_status: dict[str, int] = {}
        for row in session.execute(
            select(
                CustomUserCustomuser.payement_status,
                func.count(CustomUserCustomuser.id),
            )
            .where(*customer_filter)
            .group_by(CustomUserCustomuser.payement_status)
        ).all():
            payment_status[row[0] or "unknown"] = row[1]

        # Signups per week (last 12 weeks).
        since_12w = now - datetime.timedelta(weeks=12)
        week = func.date_trunc("week", CustomUserCustomuser.date_joined)
        signups_by_week = [
            {"week": row[0].date().isoformat(), "count": row[1]}
            for row in session.execute(
                select(week, func.count(CustomUserCustomuser.id))
                .where(*customer_filter, CustomUserCustomuser.date_joined >= since_12w)
                .group_by(week)
                .order_by(week)
            ).all()
            if row[0] is not None
        ]

        active_users = session.scalar(
            select(func.count())
            .select_from(CustomUserCustomuser)
            .where(*customer_filter, CustomUserCustomuser.is_active.is_(True))
        )
        inactive_users = session.scalar(
            select(func.count())
            .select_from(CustomUserCustomuser)
            .where(*customer_filter, CustomUserCustomuser.is_active.is_(False))
        )
        customers_total = session.scalar(
            select(func.count())
            .select_from(CustomUserCustomuser)
            .where(*customer_filter)
        )

        # Zones-per-customer distribution, bucketed.
        buckets = {"0": 0, "1": 0, "2-3": 0, "4+": 0}
        users_with_zones = 0
        for row in session.execute(
            select(AnalyticsZone.user_id, func.count(AnalyticsZone.id))
            .join(
                CustomUserCustomuser, CustomUserCustomuser.id == AnalyticsZone.user_id
            )
            .where(
                CustomUserCustomuser.is_staff.is_(False),
                CustomUserCustomuser.is_technician.is_(False),
            )
            .group_by(AnalyticsZone.user_id)
        ).all():
            users_with_zones += 1
            n = row[1]
            if n == 1:
                buckets["1"] += 1
            elif n <= 3:
                buckets["2-3"] += 1
            else:
                buckets["4+"] += 1
        buckets["0"] = max((customers_total or 0) - users_with_zones, 0)

        since_30d = now - datetime.timedelta(days=30)
        alerts_by_type = [
            {"type": row[0], "count": row[1]}
            for row in session.execute(
                select(AnalyticsAlert.type, func.count(AnalyticsAlert.id).label("n"))
                .where(AnalyticsAlert.created_at >= since_30d)
                .group_by(AnalyticsAlert.type)
                .order_by(func.count(AnalyticsAlert.id).desc())
            ).all()
        ]

        # LoRaWAN device fleet health (best-effort — table may be absent).
        stale_cutoff = now - datetime.timedelta(hours=24)
        devices_total = 0
        devices_stale = 0
        try:
            for r in session.execute(
                text(
                    "SELECT dev_eui, MAX(received_at) AS last FROM lora_uplink "
                    "GROUP BY dev_eui"
                )
            ).all():
                devices_total += 1
                if r.last is None or r.last < stale_cutoff:
                    devices_stale += 1
        except Exception:  # noqa: BLE001 — table missing / db unavailable
            devices_total = devices_stale = 0

        return {
            "payment_status": payment_status,
            "signups_by_week": signups_by_week,
            "active_users": active_users,
            "inactive_users": inactive_users,
            "zones_per_user": buckets,
            "alerts_by_type": alerts_by_type,
            "devices": {
                "total": devices_total,
                "stale": devices_stale,
                "online": max(devices_total - devices_stale, 0),
            },
        }


# ---------------------------------------------------------------------------
# Device fleet health (LoRaWAN uplinks)
# ---------------------------------------------------------------------------
@router.get("/admin/devices/health", summary="Admin: device fleet health")
def device_fleet_health(user: AuthedUser = Depends(get_current_staff_user)):
    now = _utcnow()
    online_cutoff = now - datetime.timedelta(hours=24)
    stale_cutoff = now - datetime.timedelta(hours=72)

    devices: list[dict[str, Any]] = []
    with session_scope() as session:
        try:
            latest_rows = session.execute(
                text(
                    "SELECT DISTINCT ON (dev_eui) dev_eui, device_name, received_at, "
                    "battery_v, rssi, snr FROM lora_uplink "
                    "ORDER BY dev_eui, received_at DESC"
                )
            ).all()
            counts = dict(
                session.execute(
                    text(
                        "SELECT dev_eui, COUNT(*) FROM lora_uplink "
                        "WHERE received_at >= :cut GROUP BY dev_eui"
                    ),
                    {"cut": online_cutoff},
                ).all()
            )
            for r in latest_rows:
                last_seen = r.received_at
                if last_seen is None or last_seen < stale_cutoff:
                    status = "offline"
                elif last_seen < online_cutoff:
                    status = "stale"
                else:
                    status = "online"
                devices.append(
                    {
                        "dev_eui": r.dev_eui,
                        "device_name": r.device_name or "",
                        "last_seen": last_seen.isoformat() if last_seen else None,
                        "battery_v": r.battery_v,
                        "rssi": r.rssi,
                        "snr": r.snr,
                        "uplinks_24h": counts.get(r.dev_eui, 0),
                        "status": status,
                    }
                )
        except Exception:  # noqa: BLE001 — table missing / db unavailable
            devices = []

    summary = {
        "total": len(devices),
        "online": sum(1 for d in devices if d["status"] == "online"),
        "stale": sum(1 for d in devices if d["status"] == "stale"),
        "offline": sum(1 for d in devices if d["status"] == "offline"),
    }
    return {"devices": devices, "summary": summary}


# ---------------------------------------------------------------------------
# Zones CRUD per user
# ---------------------------------------------------------------------------
@router.get("/users/{username}/zones", summary="Admin: list a user's zones")
def list_user_zones(username: str, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        owner = _resolve_user(session, username)
        if owner is None:
            return _404(f"User '{username}' not found.")
        rows = session.scalars(
            select(AnalyticsZone)
            .where(AnalyticsZone.user_id == owner.id)
            .order_by(AnalyticsZone.id)
        ).all()
        return [_serialize_zone(z) for z in rows]


@router.post("/users/{username}/zones", summary="Admin: create a zone for a user")
def create_user_zone(
    username: str,
    payload: ZoneWriteIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope(commit=True) as session:
        owner = _resolve_user(session, username)
        if owner is None:
            return _404(f"User '{username}' not found.")
        data = payload.model_dump(exclude_unset=True)
        err = _validate_zone(data)
        if err is not None:
            return _400(err)
        z = AnalyticsZone(user_id=owner.id, **data)
        session.add(z)
        session.flush()
        record_audit(
            session, user.id, "zone.create", "zone", z.id, {"username": username}
        )
        return DjangoStyleJSONResponse(_serialize_zone(z), status_code=201)


def _get_user_zone(session, username: str, pk: int):
    owner = _resolve_user(session, username)
    if owner is None:
        return None, _404(f"User '{username}' not found.")
    z = session.scalars(
        select(AnalyticsZone).where(
            AnalyticsZone.user_id == owner.id, AnalyticsZone.id == pk
        )
    ).first()
    if z is None:
        return None, _404("Zone not found for this user.")
    return z, None


@router.get("/users/{username}/zones/{pk}", summary="Admin: fetch a user's zone")
def get_user_zone(
    username: str, pk: int, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope() as session:
        z, err = _get_user_zone(session, username, pk)
        if err is not None:
            return err
        return _serialize_zone(z)


def _do_update_zone(session, username, pk, payload, fields):
    z, err = _get_user_zone(session, username, pk)
    if err is not None:
        return err
    data = {
        k: v
        for k, v in payload.model_dump(exclude_unset=True).items()
        if fields is None or k in fields
    }
    bad = _validate_zone(data)
    if bad is not None:
        return _400(bad)
    for k, v in data.items():
        setattr(z, k, v)
    session.flush()
    return z


@router.put("/users/{username}/zones/{pk}")
def put_user_zone(
    username: str,
    pk: int,
    payload: ZoneWriteIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope(commit=True) as session:
        res = _do_update_zone(session, username, pk, payload, None)
        return res if isinstance(res, DjangoStyleJSONResponse) else _serialize_zone(res)


@router.patch("/users/{username}/zones/{pk}")
def patch_user_zone(
    username: str,
    pk: int,
    payload: ZoneWriteIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope(commit=True) as session:
        res = _do_update_zone(session, username, pk, payload, None)
        return res if isinstance(res, DjangoStyleJSONResponse) else _serialize_zone(res)


@router.delete("/users/{username}/zones/{pk}")
def delete_user_zone(
    username: str, pk: int, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        z, err = _get_user_zone(session, username, pk)
        if err is not None:
            return err
        session.delete(z)
        session.flush()
        record_audit(
            session, user.id, "zone.delete", "zone", pk, {"username": username}
        )
        return DjangoStyleJSONResponse(None, status_code=204)


# --- zone params subset ----------------------------------------------------
@router.get("/users/{username}/zones/{pk}/params")
def get_zone_params(
    username: str, pk: int, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope() as session:
        z, err = _get_user_zone(session, username, pk)
        if err is not None:
            return err
        return _serialize_zone_params(z)


@router.put("/users/{username}/zones/{pk}/params")
def put_zone_params(
    username: str,
    pk: int,
    payload: ZoneWriteIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope(commit=True) as session:
        res = _do_update_zone(session, username, pk, payload, ZONE_PARAMS_FIELDS)
        return (
            res
            if isinstance(res, DjangoStyleJSONResponse)
            else _serialize_zone_params(res)
        )


@router.patch("/users/{username}/zones/{pk}/params")
def patch_zone_params(
    username: str,
    pk: int,
    payload: ZoneWriteIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope(commit=True) as session:
        res = _do_update_zone(session, username, pk, payload, ZONE_PARAMS_FIELDS)
        return (
            res
            if isinstance(res, DjangoStyleJSONResponse)
            else _serialize_zone_params(res)
        )


# ---------------------------------------------------------------------------
# ActiveGraph / GraphName / SensorColor (per-user, per-zone display config)
# ---------------------------------------------------------------------------
def _column_keys(model) -> set[str]:
    return {c.key for c in model.__table__.columns}


def _get_or_create_config(session, model, user_id: int, zone_id: int):
    obj = session.scalars(
        select(model).where(model.user_id == user_id, model.zone_id == zone_id)
    ).first()
    if obj is not None:
        return obj
    # Untested/unused in prod (these paths stay on Django). Best-effort create
    # so a fastapp-first hit does not 500: booleans → True, strings → "".
    kwargs: dict[str, Any] = {}
    for c in model.__table__.columns:
        if c.key in ("id", "user_id", "zone_id") or c.primary_key:
            continue
        try:
            pt = c.type.python_type
        except NotImplementedError:
            pt = str
        kwargs[c.key] = True if pt is bool else ("" if pt is str else None)
    obj = model(user_id=user_id, zone_id=zone_id, **kwargs)
    session.add(obj)
    session.flush()
    return obj


def _resolve_zone(session, username: str, zone_id: int):
    owner = _resolve_user(session, username)
    if owner is None:
        return None, None, _404(f"User '{username}' not found.")
    z = session.scalars(
        select(AnalyticsZone).where(
            AnalyticsZone.id == zone_id, AnalyticsZone.user_id == owner.id
        )
    ).first()
    if z is None:
        return None, None, _404("Zone not found for this user.")
    return owner, z, None


async def _json_body(request: Request):
    raw = await request.body()
    if not raw:
        return {}, None
    try:
        return json.loads(raw), None
    except ValueError:
        return None, _400({"detail": "Invalid JSON body."})


@router.get("/users/{username}/zones/{zone_id}/active-graph")
def get_active_graph(
    username: str, zone_id: int, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        owner, z, err = _resolve_zone(session, username, zone_id)
        if err is not None:
            return err
        ag = _get_or_create_config(session, AnalyticsActivegraph, owner.id, z.id)
        return _serialize_active_graph(ag)


@router.patch("/users/{username}/zones/{zone_id}/active-graph")
async def patch_active_graph(
    username: str,
    zone_id: int,
    request: Request,
    user: AuthedUser = Depends(get_current_staff_user),
):
    body, err = await _json_body(request)
    if err is not None:
        return err
    with session_scope(commit=True) as session:
        owner, z, rerr = _resolve_zone(session, username, zone_id)
        if rerr is not None:
            return rerr
        ag = _get_or_create_config(session, AnalyticsActivegraph, owner.id, z.id)
        keys = _column_keys(AnalyticsActivegraph)
        for k, v in (body or {}).items():
            if k in keys:
                setattr(ag, k, v)
        session.flush()
        return _serialize_active_graph(ag)


@router.get("/users/{username}/zones/{zone_id}/graph-names")
def get_graph_names(
    username: str, zone_id: int, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        owner, z, err = _resolve_zone(session, username, zone_id)
        if err is not None:
            return err
        obj = _get_or_create_config(session, AnalyticsGraphname, owner.id, z.id)
        return _serialize_config(obj, GRAPH_NAME_FIELDS)


@router.patch("/users/{username}/zones/{zone_id}/graph-names")
async def patch_graph_names(
    username: str,
    zone_id: int,
    request: Request,
    user: AuthedUser = Depends(get_current_staff_user),
):
    body, err = await _json_body(request)
    if err is not None:
        return err
    with session_scope(commit=True) as session:
        owner, z, rerr = _resolve_zone(session, username, zone_id)
        if rerr is not None:
            return rerr
        obj = _get_or_create_config(session, AnalyticsGraphname, owner.id, z.id)
        keys = _column_keys(AnalyticsGraphname) - {"id", "user_id", "zone_id"}
        for k, v in (body or {}).items():
            if k in keys:
                setattr(obj, k, v)
        session.flush()
        record_audit(session, user.id, "graph_names.update", "graph_names", obj.id)
        return _serialize_config(obj, GRAPH_NAME_FIELDS)


@router.get("/users/{username}/zones/{zone_id}/sensor-colors")
def get_sensor_colors(
    username: str, zone_id: int, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        owner, z, err = _resolve_zone(session, username, zone_id)
        if err is not None:
            return err
        obj = _get_or_create_config(session, AnalyticsSensorcolor, owner.id, z.id)
        return _serialize_config(obj, SENSOR_COLOR_FIELDS)


@router.patch("/users/{username}/zones/{zone_id}/sensor-colors")
async def patch_sensor_colors(
    username: str,
    zone_id: int,
    request: Request,
    user: AuthedUser = Depends(get_current_staff_user),
):
    body, err = await _json_body(request)
    if err is not None:
        return err
    with session_scope(commit=True) as session:
        owner, z, rerr = _resolve_zone(session, username, zone_id)
        if rerr is not None:
            return rerr
        obj = _get_or_create_config(session, AnalyticsSensorcolor, owner.id, z.id)
        keys = _column_keys(AnalyticsSensorcolor) - {"id", "user_id", "zone_id"}
        for k, v in (body or {}).items():
            if k in keys:
                setattr(obj, k, v)
        session.flush()
        record_audit(session, user.id, "sensor_colors.update", "sensor_colors", obj.id)
        return _serialize_config(obj, SENSOR_COLOR_FIELDS)


# ---------------------------------------------------------------------------
# Alerts admin
# ---------------------------------------------------------------------------
def _serialize_alert(a: AnalyticsAlert) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "type": a.type,
        "description": a.description,
        "condition": a.condition,
        "condition_nbr": (
            float(a.condition_nbr) if a.condition_nbr is not None else None
        ),
        "sensor_key": a.sensor_key,
        "zone": a.zone_id,
        "is_active": a.is_active,
        "last_triggered_at": (
            a.last_triggered_at.isoformat() if a.last_triggered_at else None
        ),
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "user": a.user_id,
    }


def _serialize_alert_row(session, a: AnalyticsAlert) -> dict[str, Any]:
    row = _serialize_alert(a)
    row["username"] = username_for(session, a.user_id) if a.user_id else None
    return row


@router.get("/users/{username}/alerts")
def list_user_alerts(username: str, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        owner = _resolve_user(session, username)
        if owner is None:
            return _404(f"User '{username}' not found.")
        rows = session.scalars(
            select(AnalyticsAlert)
            .where(AnalyticsAlert.user_id == owner.id)
            .order_by(AnalyticsAlert.id.desc())
        ).all()
        return [_serialize_alert(a) for a in rows]


@router.get("/admin/alerts/{pk}")
def get_alert(pk: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        a = session.get(AnalyticsAlert, pk)
        if a is None:
            return _404("Not found.")
        return _serialize_alert(a)


@router.patch("/admin/alerts/{pk}")
def patch_alert(
    pk: int,
    payload: AlertAdminPatchIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope(commit=True) as session:
        a = session.get(AnalyticsAlert, pk)
        if a is None:
            return _404("Not found.")
        if payload.is_active is not None:
            a.is_active = payload.is_active
            a.updated_at = _utcnow()
            session.flush()
        return _serialize_alert(a)


@router.delete("/admin/alerts/{pk}")
def delete_alert(pk: int, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope(commit=True) as session:
        a = session.get(AnalyticsAlert, pk)
        if a is None:
            return _404("Not found.")
        session.delete(a)
        session.flush()
        return DjangoStyleJSONResponse(None, status_code=204)


@router.get("/admin/alerts", summary="Admin: all alerts (filtered)")
def list_all_alerts(
    username: str | None = None,
    type: str | None = None,
    sensor_key: str | None = None,
    is_active: bool | None = None,
    zone_id: int | None = None,
    user: AuthedUser = Depends(get_current_staff_user),
):
    with session_scope() as session:
        criteria = []
        if username:
            criteria.append(
                AnalyticsAlert.user_id
                == (
                    session.scalar(
                        select(CustomUserCustomuser.id).where(
                            CustomUserCustomuser.username == username
                        )
                    )
                    or -1
                )
            )
        if type:
            criteria.append(AnalyticsAlert.type == type)
        if sensor_key:
            criteria.append(AnalyticsAlert.sensor_key == sensor_key)
        if is_active is not None:
            criteria.append(AnalyticsAlert.is_active.is_(is_active))
        if zone_id is not None:
            criteria.append(AnalyticsAlert.zone_id == zone_id)
        rows = session.scalars(
            select(AnalyticsAlert)
            .where(*criteria)
            .order_by(AnalyticsAlert.id.desc())
            .limit(500)
        ).all()
        return [_serialize_alert_row(session, a) for a in rows]


@router.post("/admin/alerts", summary="Admin: create an alert")
def create_alert(
    payload: AlertAdminCreateIn, user: AuthedUser = Depends(get_current_staff_user)
):
    with session_scope(commit=True) as session:
        owner = _resolve_user(session, payload.username)
        if owner is None:
            return _404(f"User '{payload.username}' not found.")
        zone_id = None
        if payload.zone_id is not None:
            zone = session.scalars(
                select(AnalyticsZone).where(
                    AnalyticsZone.id == payload.zone_id,
                    AnalyticsZone.user_id == owner.id,
                )
            ).first()
            if zone is None:
                return _404(f"Zone {payload.zone_id} not found for this user.")
            zone_id = zone.id
        now = _utcnow()
        alert = AnalyticsAlert(
            user_id=owner.id,
            zone_id=zone_id,
            name=payload.name,
            type=payload.type,
            description=payload.description or "",
            condition=payload.condition,
            condition_nbr=payload.condition_nbr,
            sensor_key=payload.sensor_key or "",
            is_active=payload.is_active,
            # Django field defaults (app-level; the columns are NOT NULL with no
            # DB default, so they must be supplied explicitly here).
            notify_email=True,
            notify_whatsapp=False,
            notify_sms=False,
            created_at=now,
            updated_at=now,
        )
        session.add(alert)
        session.flush()
        record_audit(
            session,
            user.id,
            "alert.create",
            "alert",
            alert.id,
            {"username": payload.username, "name": payload.name},
        )
        return _serialize_alert_row(session, alert)


@router.get("/admin/alert-analytics", summary="Admin: alert distributions")
def alerts_analytics(user: AuthedUser = Depends(get_current_staff_user)):
    now = _utcnow()
    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(AnalyticsAlert))
        active = session.scalar(
            select(func.count())
            .select_from(AnalyticsAlert)
            .where(AnalyticsAlert.is_active.is_(True))
        )
        by_type = [
            {"type": row[0], "count": row[1]}
            for row in session.execute(
                select(AnalyticsAlert.type, func.count(AnalyticsAlert.id).label("n"))
                .group_by(AnalyticsAlert.type)
                .order_by(func.count(AnalyticsAlert.id).desc())
            ).all()
        ]
        by_sensor = [
            {"sensor_key": row[0], "count": row[1]}
            for row in session.execute(
                select(
                    AnalyticsAlert.sensor_key, func.count(AnalyticsAlert.id).label("n")
                )
                .group_by(AnalyticsAlert.sensor_key)
                .order_by(func.count(AnalyticsAlert.id).desc())
                .limit(15)
            ).all()
        ]
        triggered_ever = session.scalar(
            select(func.count())
            .select_from(AnalyticsAlert)
            .where(AnalyticsAlert.last_triggered_at.is_not(None))
        )
        recently_triggered = [
            _serialize_alert_row(session, a)
            for a in session.scalars(
                select(AnalyticsAlert)
                .where(
                    AnalyticsAlert.last_triggered_at >= now - datetime.timedelta(days=7)
                )
                # id.desc() is a deterministic tie-break so rows sharing an identical
                # last_triggered_at come out in the same order as the Django surface.
                .order_by(
                    AnalyticsAlert.last_triggered_at.desc(), AnalyticsAlert.id.desc()
                )
                .limit(10)
            ).all()
        ]
        return {
            "total": total,
            "active": active,
            "inactive": total - active,
            "triggered_ever": triggered_ever,
            "by_type": by_type,
            "by_sensor": by_sensor,
            "recently_triggered": recently_triggered,
        }


# ---------------------------------------------------------------------------
# Per-user activity timeline
# ---------------------------------------------------------------------------
@router.get("/users/{username}/activity")
def user_activity(username: str, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        owner = _resolve_user(session, username)
        if owner is None:
            return _404(f"User '{username}' not found.")
        events: list[dict[str, Any]] = []
        if owner.date_joined:
            events.append(
                {
                    "kind": "joined",
                    "label": "Compte créé",
                    "at": owner.date_joined.isoformat(),
                }
            )
        if owner.last_login:
            events.append(
                {
                    "kind": "login",
                    "label": "Dernière connexion",
                    "at": owner.last_login.isoformat(),
                }
            )
        if owner.last_notified:
            events.append(
                {
                    "kind": "notified",
                    "label": "Dernière notification envoyée",
                    "at": owner.last_notified.isoformat(),
                }
            )
        zone_count = session.scalar(
            select(func.count())
            .select_from(AnalyticsZone)
            .where(AnalyticsZone.user_id == owner.id)
        )
        events.append(
            {"kind": "zones", "label": f"{zone_count} zone(s) actives", "at": None}
        )
        triggered = session.execute(
            select(AnalyticsAlert.name, AnalyticsAlert.last_triggered_at)
            .where(
                AnalyticsAlert.user_id == owner.id,
                AnalyticsAlert.last_triggered_at.is_not(None),
            )
            .order_by(AnalyticsAlert.last_triggered_at.desc())
            .limit(5)
        ).all()
        for row in triggered:
            events.append(
                {
                    "kind": "alert",
                    "label": f"Alerte déclenchée : {row[0]}",
                    "at": row[1].isoformat(),
                }
            )
        events.sort(key=lambda e: e["at"] or "", reverse=True)
        return {"events": events}


# ---------------------------------------------------------------------------
# Per-user sensor units
# ---------------------------------------------------------------------------
@router.get("/users/{username}/sensor-units")
def get_sensor_units(username: str, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        owner = _resolve_user(session, username)
        if owner is None:
            return _404(f"User '{username}' not found.")
        rows = session.scalars(
            select(AnalyticsUsersensorunitpreference)
            .where(AnalyticsUsersensorunitpreference.user_id == owner.id)
            .order_by(AnalyticsUsersensorunitpreference.id)
        ).all()
        return {r.sensor_key: r.unit for r in rows}


@router.patch("/users/{username}/sensor-units")
async def patch_sensor_units(
    username: str, request: Request, user: AuthedUser = Depends(get_current_staff_user)
):
    body, err = await _json_body(request)
    if err is not None:
        return err
    if not isinstance(body, dict):
        return _400({"detail": "Body must be a JSON object of sensor_key → unit."})
    for sensor_key, unit in body.items():
        if not isinstance(sensor_key, str) or not isinstance(unit, str):
            return _400({"detail": "All keys and values must be strings."})
    with session_scope(commit=True) as session:
        owner = _resolve_user(session, username)
        if owner is None:
            return _404(f"User '{username}' not found.")
        for sensor_key, unit in body.items():
            existing = session.scalars(
                select(AnalyticsUsersensorunitpreference).where(
                    AnalyticsUsersensorunitpreference.user_id == owner.id,
                    AnalyticsUsersensorunitpreference.sensor_key == sensor_key,
                )
            ).first()
            if existing is not None:
                existing.unit = unit
                existing.updated_at = _utcnow()
            else:
                session.add(
                    AnalyticsUsersensorunitpreference(
                        user_id=owner.id,
                        sensor_key=sensor_key,
                        unit=unit,
                        updated_at=_utcnow(),
                    )
                )
        session.flush()
        rows = session.scalars(
            select(AnalyticsUsersensorunitpreference)
            .where(AnalyticsUsersensorunitpreference.user_id == owner.id)
            .order_by(AnalyticsUsersensorunitpreference.id)
        ).all()
        return {r.sensor_key: r.unit for r in rows}
