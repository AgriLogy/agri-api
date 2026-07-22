"""fastapp /admin/sensor-data — generic sensor-reading explorer (staff only).

Strangler port of ``apps/sensors/router_sensor_data.py`` (django-ninja). One
generic surface over the sensor reading models so the back-office can inspect,
correct and clean raw time-series for ANY user/zone. Reads/writes go through
the agri-core SQLAlchemy session (no Django ORM); mutations are audited.

Byte-parity with the Django version: same catalog order + humanized labels,
same row shape (``id``/``value``/``timestamp``/``zone_id``/``username``, with
``timestamp`` as ``.isoformat()``), same 404/400 envelopes, and the same guard
on the bulk range-delete. ``NpkSensor`` has no single ``value`` column, so its
rows return ``value: null`` and it is not editable (matches Django).
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Date, cast, delete, select

from agri.core.database import session_scope
from agri.core.database.client import _owner_user, _owner_zone, _with_device_join
from fastapp.adminutil import record_audit, username_for
from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.json import DjangoStyleJSONResponse
from fastapp.sensors import SENSOR_SPEC, agri_db_model, effective_owner

router = APIRouter(tags=["admin-sensor-data"])

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000

# slug → Django sensor class name (SENSOR_MODELS order, mirrored by SENSOR_SPEC).
# The catalog label is the humanized class name, so we carry the class name to
# reproduce ``_humanize(model.__name__)`` byte-for-byte.
_SLUG_TO_CLASSNAME: dict[str, str] = {
    "precipitationrate": "PrecipitationRate",
    "humidityweather": "HumidityWeather",
    "windspeed": "WindSpeed",
    "solarradiation": "SolarRadiation",
    "pressureweather": "PressureWeather",
    "winddirection": "WindDirection",
    "temperatureweather": "TemperatureWeather",
    "ecsoilmedium": "ECSoilMedium",
    "soiltemperaturemedium": "SoilTemperatureMedium",
    "ecsoilhigh": "ECSoilHigh",
    "ecsoillow": "ECSoilLow",
    "soilmoisturemedium": "SoilMoistureMedium",
    "soilmoisturehigh": "SoilMoistureHigh",
    "soilmoisturelow": "SoilMoistureLow",
    "phsoil": "PhSoil",
    "soiltemperaturelow": "SoilTemperatureLow",
    "soiltemperaturehigh": "SoilTemperatureHigh",
    "waterflow": "WaterFlowSensor",
    "waterec": "WaterECSensor",
    "phwater": "PhWaterSensor",
    "electricityconsumption": "ElectricityConsumptionSensor",
    "leafmoisture": "LeafMoistureSensor",
    "multidepthsoilmoisture": "MultiDepthSoilMoistureSensor",
    "largefruitdiameter": "LargeFruitDiameterSensor",
    "waterlevel": "WaterLevelSensor",
    "soilconductivity": "SoilConductivitySensor",
    "et0weather": "Et0Weather",
    "et0calculated": "Et0Calculated",
    "vpdweather": "VPDWeather",
    "leaftemperature": "LeafTemperatureSensor",
    "soilsalinity": "SoilSalinitySensor",
    "npk": "NpkSensor",
    "fruitsize": "FruitSizeSensor",
    "ecsalinity": "EcSalinitySensor",
    "waterpressure": "WaterPressureSensor",
    "battery": "BatterySensor",
    "signal": "SignalSensor",
}


def _model_for(sensor: str):
    spec = SENSOR_SPEC.get(sensor)
    return agri_db_model(spec) if spec is not None else None


def _humanize(name: str) -> str:
    """`SoilMoistureHigh` -> `Soil Moisture High` (port of the Django helper)."""
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper():
            out.append(" ")
        out.append(ch)
    return "".join(out)


def _parse_dt(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _range_criteria(model, frm: str | None, to: str | None) -> list:
    """Mirror the Django ``_apply_range``: prefer a full datetime bound, else
    fall back to a date-only comparison on the timestamp."""
    criteria = []
    if frm:
        dt = _parse_dt(frm)
        if dt is not None:
            criteria.append(model.timestamp >= dt)
        else:
            d = _parse_date(frm)
            if d is not None:
                criteria.append(cast(model.timestamp, Date) >= d)
    if to:
        dt = _parse_dt(to)
        if dt is not None:
            criteria.append(model.timestamp <= dt)
        else:
            d = _parse_date(to)
            if d is not None:
                criteria.append(cast(model.timestamp, Date) <= d)
    return criteria


def _row(
    session, obj, eff_user: int | None = None, eff_zone: int | None = None
) -> dict:
    """One wire row. ``zone_id``/``username`` report the EFFECTIVE (device-
    resolved) owner so the projection agrees with the filter — the raw columns
    are a stale commissioning snapshot and would label the row with a
    different client than the one the caller filtered on."""
    ts = getattr(obj, "timestamp", None)
    if eff_user is None and eff_zone is None:
        eff_user, eff_zone = effective_owner(session, obj)
    return {
        "id": obj.id,
        "value": getattr(obj, "value", None),
        "timestamp": ts.isoformat() if ts else None,
        "zone_id": eff_zone,
        "username": username_for(session, eff_user),
    }


class ValueIn(BaseModel):
    value: float | None = None


@router.get(
    "/admin/sensor-data/catalog", summary="Admin: sensor catalog (slug + label)"
)
def catalog(user: AuthedUser = Depends(get_current_staff_user)):
    return {
        "sensors": [
            {"slug": slug, "label": _humanize(_SLUG_TO_CLASSNAME[slug])}
            for slug in SENSOR_SPEC
        ]
    }


@router.get("/admin/sensor-data", summary="Admin: list sensor readings")
def list_readings(
    sensor: str,
    username: str | None = None,
    zone_id: int | None = None,
    frm: str | None = Query(None, alias="from"),
    to: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: AuthedUser = Depends(get_current_staff_user),
):
    model = _model_for(sensor)
    if model is None:
        return DjangoStyleJSONResponse({"detail": "Unknown sensor"}, status_code=404)
    limit = max(1, min(limit, MAX_LIMIT))
    with session_scope() as session:
        criteria = []
        if username:
            from agri.db.users import CustomUserCustomuser

            uid = session.scalar(
                select(CustomUserCustomuser.id).where(
                    CustomUserCustomuser.username == username
                )
            )
            # device-keyed ownership: resolve via analytics_device (COALESCE).
            criteria.append(_owner_user(model) == (uid if uid is not None else -1))
        if zone_id is not None:
            criteria.append(_owner_zone(model) == zone_id)
        criteria += _range_criteria(model, frm, to)
        results = session.execute(
            _with_device_join(
                select(
                    model,
                    _owner_user(model).label("eff_user"),
                    _owner_zone(model).label("eff_zone"),
                ),
                model,
            )
            .where(*criteria)
            .order_by(model.timestamp.desc())
            .limit(limit)
        ).all()
        rows = [
            _row(session, o, eff_user, eff_zone) for o, eff_user, eff_zone in results
        ]
        return {"sensor": sensor, "count": len(rows), "rows": rows}


@router.patch(
    "/admin/sensor-data/{sensor}/{row_id}", summary="Admin: correct a reading"
)
def patch_reading(
    sensor: str,
    row_id: int,
    payload: ValueIn,
    user: AuthedUser = Depends(get_current_staff_user),
):
    model = _model_for(sensor)
    if model is None:
        return DjangoStyleJSONResponse({"detail": "Unknown sensor"}, status_code=404)
    with session_scope(commit=True) as session:
        obj = session.get(model, row_id)
        if obj is None:
            return DjangoStyleJSONResponse({"detail": "Not found"}, status_code=404)
        if not hasattr(obj, "value"):
            return DjangoStyleJSONResponse(
                {"detail": "Sensor has no editable value"}, status_code=400
            )
        obj.value = payload.value
        session.flush()
        record_audit(
            session,
            user.id,
            "sensor_data.update",
            sensor,
            row_id,
            {"value": payload.value},
        )
        return _row(session, obj)


@router.delete(
    "/admin/sensor-data/{sensor}/{row_id}", summary="Admin: delete one reading"
)
def delete_reading(
    sensor: str, row_id: int, user: AuthedUser = Depends(get_current_staff_user)
):
    model = _model_for(sensor)
    if model is None:
        return DjangoStyleJSONResponse({"detail": "Unknown sensor"}, status_code=404)
    with session_scope(commit=True) as session:
        obj = session.get(model, row_id)
        if obj is None:
            return DjangoStyleJSONResponse({"detail": "Not found"}, status_code=404)
        session.delete(obj)
        record_audit(session, user.id, "sensor_data.delete", sensor, row_id)
        return {"status": "deleted"}


@router.delete("/admin/sensor-data/{sensor}", summary="Admin: bulk-delete a range")
def delete_range(
    sensor: str,
    username: str | None = None,
    zone_id: int | None = None,
    frm: str | None = Query(None, alias="from"),
    to: str | None = None,
    user: AuthedUser = Depends(get_current_staff_user),
):
    model = _model_for(sensor)
    if model is None:
        return DjangoStyleJSONResponse({"detail": "Unknown sensor"}, status_code=404)
    # Guard against nuking a whole sensor: require a zone AND a time bound.
    if zone_id is None or (not frm and not to):
        return DjangoStyleJSONResponse(
            {"detail": "zone_id and at least one of from/to are required"},
            status_code=400,
        )
    with session_scope(commit=True) as session:
        # Same predicates as ``list_readings`` — ownership resolves through the
        # device JOIN, never the raw columns, so a range-delete removes EXACTLY
        # the rows the equivalent list call returns.
        criteria = [_owner_zone(model) == zone_id]
        if username:
            from agri.db.users import CustomUserCustomuser

            uid = session.scalar(
                select(CustomUserCustomuser.id).where(
                    CustomUserCustomuser.username == username
                )
            )
            criteria.append(_owner_user(model) == (uid if uid is not None else -1))
        criteria += _range_criteria(model, frm, to)
        # DELETE can't carry the outer join → resolve ids in a subquery that
        # does (``correlate(None)`` keeps it standalone, not correlated to the
        # DELETE's own target table).
        target_ids = (
            _with_device_join(select(model.id), model).where(*criteria).correlate(None)
        )
        deleted = session.execute(
            delete(model).where(model.id.in_(target_ids))
        ).rowcount
        record_audit(
            session,
            user.id,
            "sensor_data.range_delete",
            sensor,
            zone_id,
            {"from": frm, "to": to, "username": username, "deleted": deleted},
        )
        return {"status": "deleted", "deleted": deleted}
