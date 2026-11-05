"""HTTP tests for the farm-map router (agri-api#459, Phase 1).

Dual-ORM: needs Postgres (fastapp reads via SQLAlchemy the rows Django writes)
+ committed data (``django_db(transaction=True)``). The farm-map tables are
created by ``conftest._create_farm_map_schema``.
"""

from __future__ import annotations

import datetime as dt

import pytest
from agri.core.geometry import area_hectares
from django.conf import settings as dj_settings
from django.db import connection
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.schema_compat import (
    reset_farm_map_cache,
    reset_sector_geometry_cache,
)
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM: fastapp reads the test DB Django writes (needs Postgres)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture(autouse=True)
def _clean_farm_map_tables():
    """These tables are not Django-modelled, so Django's flush leaves them
    alone; clear them around each test and re-probe the schema."""
    reset_farm_map_cache()
    reset_sector_geometry_cache()
    yield
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM analytics_sensorinstallation")
        cursor.execute("DELETE FROM analytics_mappreference")
    reset_farm_map_cache()


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


def _user(django_user_model, set_access_level, username, level="editor"):
    user = django_user_model.objects.create_user(
        username=username, email=f"{username}@example.com", password="irrelevant-3921"
    )
    set_access_level(user, level)
    return user


@pytest.fixture
def owner(django_user_model, set_access_level):
    return _user(django_user_model, set_access_level, "map-owner")


@pytest.fixture
def other(django_user_model, set_access_level):
    return _user(django_user_model, set_access_level, "map-other")


def _auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


def _zone(user, name="Z1"):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=user,
        name=name,
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
        elevation_m=120.0,
    )


def _sensor(fast, user, **over):
    body = {
        "name": "SM-01",
        "sensor_type": "soil_moisture",
        "latitude": 32.89,
        "longitude": -6.91,
    }
    body.update(over)
    return fast.post("/sensor-installations", json=body, headers=_auth(user))


def test_installation_crud(fast, owner):
    zone = _zone(owner)
    sector = fast.post(
        "/sectors", json={"name": "Block A"}, headers=_auth(owner)
    ).json()

    created = _sensor(
        fast,
        owner,
        sector_id=sector["id"],
        zone_id=zone.id,
        sensor_key="soil_moisture_medium",
        installation_depth_cm=30,
        installation_date="2026-10-01",
        maintenance_status="ok",
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "SM-01"
    assert body["sector_id"] == sector["id"]
    assert body["installation_date"] == "2026-10-01"

    listed = fast.get("/sensor-installations", headers=_auth(owner)).json()
    assert [s["id"] for s in listed] == [body["id"]]

    moved = fast.patch(
        f"/sensor-installations/{body['id']}",
        json={"name": "  SM-North ", "latitude": 32.9},
        headers=_auth(owner),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["name"] == "SM-North"
    assert moved.json()["latitude"] == 32.9
    # Untouched fields survive a partial update.
    assert moved.json()["installation_depth_cm"] == 30

    gone = fast.delete(f"/sensor-installations/{body['id']}", headers=_auth(owner))
    assert gone.status_code == 200
    assert fast.get("/sensor-installations", headers=_auth(owner)).json() == []


def test_client_ref_import_is_idempotent(fast, owner):
    first = _sensor(fast, owner, client_ref="sensor-1720000000-abc")
    again = _sensor(fast, owner, client_ref="sensor-1720000000-abc", name="dup")
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["name"] == "SM-01"
    assert len(fast.get("/sensor-installations", headers=_auth(owner)).json()) == 1


def test_installations_are_owner_scoped(fast, owner, other):
    mine = _sensor(fast, owner).json()
    assert fast.get("/sensor-installations", headers=_auth(other)).json() == []
    assert (
        fast.patch(
            f"/sensor-installations/{mine['id']}",
            json={"name": "x"},
            headers=_auth(other),
        ).status_code
        == 404
    )
    assert (
        fast.delete(
            f"/sensor-installations/{mine['id']}", headers=_auth(other)
        ).status_code
        == 404
    )


def test_foreign_sector_or_zone_is_rejected(fast, owner, other):
    their_zone = _zone(other, "theirs")
    their_sector = fast.post(
        "/sectors", json={"name": "Theirs"}, headers=_auth(other)
    ).json()
    assert _sensor(fast, owner, zone_id=their_zone.id).status_code == 400
    assert _sensor(fast, owner, sector_id=their_sector["id"]).status_code == 400


def test_null_required_field_on_patch_is_400(fast, owner):
    mine = _sensor(fast, owner).json()
    resp = fast.patch(
        f"/sensor-installations/{mine['id']}",
        json={"latitude": None},
        headers=_auth(owner),
    )
    assert resp.status_code == 400


def test_monitor_tier_cannot_write(fast, django_user_model, set_access_level):
    monitor = _user(django_user_model, set_access_level, "map-monitor", level="monitor")
    assert _sensor(fast, monitor).status_code == 403


def test_map_preferences_round_trip_and_partial_update(fast, owner):
    default = fast.get("/map-preferences", headers=_auth(owner)).json()
    assert default == {"basemap": "satellite_labels", "layers": {}, "camera": None}

    saved = fast.put(
        "/map-preferences",
        json={
            "basemap": "satellite",
            "layers": {"sensors": {"visible": False, "opacity": 0.5}},
            "camera": {"center": [-6.91, 32.89], "zoom": 16.5},
        },
        headers=_auth(owner),
    )
    assert saved.status_code == 200, saved.text

    # Saving only the camera keeps basemap + layers.
    fast.put(
        "/map-preferences",
        json={"camera": {"center": [-6.9, 32.8], "zoom": 14}},
        headers=_auth(owner),
    )
    got = fast.get("/map-preferences", headers=_auth(owner)).json()
    assert got["basemap"] == "satellite"
    assert got["layers"] == {"sensors": {"visible": False, "opacity": 0.5}}
    assert got["camera"]["center"] == [-6.9, 32.8]
    assert got["camera"]["zoom"] == 14


def test_sector_crop_metadata(fast, owner):
    created = fast.post(
        "/sectors",
        json={
            "name": "Avocado 01",
            "crop": "Avocado",
            "variety": "Hass",
            "planting_date": "2024-03-15",
            "irrigation_type": "drip",
            "root_depth_cm": 60,
        },
        headers=_auth(owner),
    )
    assert created.status_code == 201, created.text
    s = created.json()
    assert (s["crop"], s["variety"], s["planting_date"]) == (
        "Avocado",
        "Hass",
        "2024-03-15",
    )
    assert s["irrigation_type"] == "drip" and s["is_active"] is True

    # A plain rename leaves the metadata alone.
    renamed = fast.patch(
        f"/sectors/{s['id']}", json={"name": "Avocado North"}, headers=_auth(owner)
    ).json()
    assert renamed["crop"] == "Avocado" and renamed["root_depth_cm"] == 60

    # Explicit null clears one field; is_active can be switched off.
    cleared = fast.patch(
        f"/sectors/{s['id']}",
        json={"name": "Avocado North", "variety": None, "is_active": False},
        headers=_auth(owner),
    ).json()
    assert cleared["variety"] is None and cleared["is_active"] is False


def test_status_reports_values_health_alerts_and_rollup(fast, owner):
    from apps.alerts.models import Alert
    from apps.sensors.models import BatterySensor, SoilMoistureMedium

    zone = _zone(owner)
    sector = fast.post(
        "/sectors", json={"name": "Block A"}, headers=_auth(owner)
    ).json()
    now = dt.datetime.now(dt.timezone.utc)
    SoilMoistureMedium.objects.create(
        user=owner, zone=zone, value=12.5, timestamp=now - dt.timedelta(minutes=20)
    )
    BatterySensor.objects.create(
        user=owner, zone=zone, value=3.7, timestamp=now - dt.timedelta(minutes=20)
    )

    live = _sensor(
        fast,
        owner,
        name="SM-01",
        sector_id=sector["id"],
        zone_id=zone.id,
        sensor_key="soil_moisture_medium",
    ).json()
    silent = _sensor(
        fast,
        owner,
        name="FLOW-01",
        sensor_type="flow_meter",
        sector_id=sector["id"],
        zone_id=zone.id,
        sensor_key="water_pressure",
    ).json()
    unlinked = _sensor(fast, owner, name="WS-01", sensor_type="weather_station").json()

    status = fast.get("/farm-map/status", headers=_auth(owner)).json()
    by_id = {i["id"]: i for i in status["installations"]}

    assert by_id[live["id"]]["status"] == "normal"
    assert by_id[live["id"]]["latest"]["value"] == 12.5
    assert by_id[live["id"]]["latest"]["unit"] is not None
    assert by_id[live["id"]]["battery"]["value"] == 3.7
    assert by_id[silent["id"]]["status"] == "no_data"
    assert by_id[silent["id"]]["latest"] is None  # never a made-up value
    assert by_id[unlinked["id"]]["status"] == "unlinked"

    (sec,) = [s for s in status["sectors"] if s["id"] == sector["id"]]
    assert sec["status"] == "normal" and sec["sensor_count"] == 2

    # A triggered alert on the live stream turns it (and its sector) critical.
    Alert.objects.create(
        user=owner,
        zone=zone,
        name="dry",
        type="threshold",
        description="d",
        condition="<",
        condition_nbr=20,
        sensor_key="soil_moisture_medium",
        is_active=True,
    )
    status = fast.get("/farm-map/status", headers=_auth(owner)).json()
    by_id = {i["id"]: i for i in status["installations"]}
    assert by_id[live["id"]]["status"] == "critical"
    assert [a["name"] for a in by_id[live["id"]]["alerts"]] == ["dry"]
    (sec,) = [s for s in status["sectors"] if s["id"] == sector["id"]]
    assert sec["status"] == "critical" and sec["alert_count"] == 1


def test_status_is_owner_scoped(fast, owner, other):
    _sensor(fast, owner)
    assert (
        fast.get("/farm-map/status", headers=_auth(other)).json()["installations"] == []
    )


_SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[-7.95, 32.20], [-7.94, 32.20], [-7.94, 32.21], [-7.95, 32.21], [-7.95, 32.20]]
    ],
}


def test_sector_geometry_round_trips_through_http(fast, owner):
    """Regression: with the shape columns present (as on prod) /sectors must
    store and return the polygon even though the pinned agri-db ORM model does
    not map those columns — it used to 500 on read and drop the shape on write.
    """
    created = fast.post(
        "/sectors",
        json={"name": "Drawn", "geometry": _SQUARE, "color": "#16a34a"},
        headers=_auth(owner),
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]

    listed = fast.get("/sectors", headers=_auth(owner))
    assert listed.status_code == 200, listed.text
    (row,) = [s for s in listed.json() if s["id"] == sid]
    assert row["geometry"]["type"] == "Polygon"
    assert row["color"] == "#16a34a"
    assert row["area_ha"] == pytest.approx(area_hectares(_SQUARE), rel=1e-6)
    assert row["perimeter_m"] > 0
    assert row["geometry_updated_at"]

    # A rename keeps the shape; an explicit null erases it.
    renamed = fast.patch(
        f"/sectors/{sid}", json={"name": "Renamed"}, headers=_auth(owner)
    )
    assert renamed.json()["geometry"] == row["geometry"]
    erased = fast.patch(
        f"/sectors/{sid}",
        json={"name": "Renamed", "geometry": None},
        headers=_auth(owner),
    )
    assert erased.json()["geometry"] is None and erased.json()["area_ha"] is None
