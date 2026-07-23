"""fastapp /sensors — sensor-key catalog + per-sensor readings + patch.

Strangler port of ``apps/sensors/router_sensors.py`` (django-ninja). Byte-
parity with the Django version:

* ``GET /sensors`` — the sorted SENSOR_KEY_REGISTRY catalog.
* ``GET /sensors/<slug>`` — hourly-averaged readings (``raw=true`` for the
  un-aggregated native cadence); same key order + unit metadata as Django.
* ``PATCH /sensors/<slug>`` — partial update by ``id`` (owner-scoped), same
  ``_serialize_reading`` response, same 404 ``{"error": "Not found"}``.

Data access is SQLAlchemy via agri-core (no Django ORM). Only slugs in
``SENSOR_SPEC`` exist (Django registers a route per model → unknown slug is a
404), so ``{slug}`` is validated against the spec.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Body, Depends, HTTPException

from agri.core.alerts import SENSOR_KEY_REGISTRY
from agri.core.database import session_scope
from fastapp import sensors
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["sensors"])


def _parse_date(value: str | None) -> datetime.date | None:
    """Match Django's parse_date: 'YYYY-MM-DD' → date, anything else → None."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


@router.get("/sensors", summary="Sensor-key catalog (slug + unit + label + type)")
def list_sensor_catalog(user: AuthedUser = Depends(get_current_user)):
    return {
        "keys": [
            {
                "key": key,
                "unit": meta["unit"],
                "label": meta["label"],
                "type": meta.get("type"),
            }
            for key, meta in sorted(SENSOR_KEY_REGISTRY.items())
        ]
    }


@router.get("/sensors/{slug}", summary="List a sensor's readings")
def list_readings(
    slug: str,
    start_date: str | None = None,
    end_date: str | None = None,
    zone: int | None = None,
    raw: bool = False,
    user: AuthedUser = Depends(get_current_user),
):
    spec = sensors.SENSOR_SPEC.get(slug)
    if spec is None:
        raise HTTPException(status_code=404, detail="Not Found")
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if raw:
        return sensors.raw_readings(
            spec, user_id=user.id, zone_id=zone, start=start, end=end
        )
    return sensors.hourly_readings(
        spec, user_id=user.id, zone_id=zone, start=start, end=end
    )


@router.patch("/sensors/{slug}", summary="Patch one sensor row by id")
def patch_reading(
    slug: str,
    body: dict = Body(...),
    user: AuthedUser = Depends(get_current_user),
):
    spec = sensors.SENSOR_SPEC.get(slug)
    if spec is None:
        raise HTTPException(status_code=404, detail="Not Found")
    row_id = body.get("id")
    model = sensors.agri_db_model(spec)
    with session_scope(commit=True) as session:
        row = session.get(model, row_id)
        # Ownership is device-keyed: authorise against the EFFECTIVE owner
        # (the row's device), not its stale commissioning snapshot — otherwise
        # a farmer who owns a transferred device gets a 404 on his own history.
        eff_user, eff_zone = (
            sensors.effective_owner(session, row) if row is not None else (None, None)
        )
        if row is None or eff_user != user.id:
            # ninja returns Response({"error": "Not found"}, status=404) — a
            # bare {"error": ...}, NOT wrapped in {"detail": ...}.
            return DjangoStyleJSONResponse({"error": "Not found"}, status_code=404)
        for key, value in (body or {}).items():
            if key == "id":
                continue
            if hasattr(row, key):
                setattr(row, key, value)
        session.flush()
        return sensors.serialize_raw(row, spec, eff_user=eff_user, eff_zone=eff_zone)
