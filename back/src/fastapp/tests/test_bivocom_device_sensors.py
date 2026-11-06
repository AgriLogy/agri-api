"""Bivocom data-driven ingest + device-sensor mapping CRUD (#452, #284).

``POST /ingest/bivocom`` used to validate and 202 without writing anything. It
now resolves the gateway's ``device_id`` to a registered ``analytics_device``
(``serial``) and routes each wire tag through that device's active
``analytics_devicesensor`` mapping to a sensor_key + zone.

Dual-ORM: Postgres only + committed rows (fastapp writes over its own
SQLAlchemy connection; assertions read back through the Django ORM).
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from django.db import connection
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp import ingest
from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM: fastapp writes the test DB Django reads (needs Postgres)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

TS = "2026-11-06T08:00:00Z"


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture(autouse=True)
def _clean_unmanaged_tables():
    """analytics_device is unmanaged and analytics_devicesensor is not
    Django-modelled, so Django's per-test flush leaves both alone."""
    yield
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM analytics_devicesensor")
        cursor.execute("DELETE FROM analytics_device")


@pytest.fixture
def dispatched(monkeypatch):
    """Record alert dispatches instead of enqueueing Celery tasks."""
    calls: list[dict] = []

    def _record(session, **kw):
        calls.append(kw)
        return 0

    monkeypatch.setattr(ingest, "dispatch_alerts_for_reading", _record)
    return calls


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(django_user_model, set_access_level):
    user = django_user_model.objects.create_user(
        username="bv-admin", email="bv-admin@example.com", password="irrelevant-3921"
    )
    set_access_level(user, "admin")
    return user


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="bv-owner", email="bv-owner@example.com", password="irrelevant-3921"
    )


@pytest.fixture
def stranger(django_user_model):
    return django_user_model.objects.create_user(
        username="bv-stranger",
        email="bv-stranger@example.com",
        password="irrelevant-3921",
    )


def _zone(user, name):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=user,
        name=name,
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


@pytest.fixture
def zone(owner):
    return _zone(owner, "Parcelle Nord")


@pytest.fixture
def device(owner, zone):
    from apps.irrigation.models import Device

    return Device.objects.create(
        user=owner, zone=zone, device_type="bivocom", serial="router-user-R02"
    )


def _map(device, tag, sensor_key, *, zone_id=None, is_active=True):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO analytics_devicesensor "
            "(device_id, tag_name, sensor_key, zone_id, is_active) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            [device.id, tag, sensor_key, zone_id, is_active],
        )
        return cursor.fetchone()[0]


def _values(sensor_key, **filt):
    from apps.alerts.engine import get_sensor_model

    model = get_sensor_model(sensor_key)
    return list(
        model.objects.filter(**filt).order_by("id").values_list("value", flat=True)
    )


def _uplink(fast, serial, tags):
    return fast.post(
        "/ingest/bivocom", json={"device_id": serial, "timestamp": TS, "tags": tags}
    )


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


# ===========================================================================
# Ingest
# ===========================================================================
def test_mapped_tags_persist_with_device_id_and_dispatch(
    fast, device, zone, owner, dispatched
):
    _map(device, "ta", "temperature_weather")
    _map(device, "ha", "humidity_weather")

    res = _uplink(fast, device.serial, {"ta": 22.5, "ha": 64.0, "xx": 1.0})

    assert res.status_code == 201, res.text
    assert res.json() == {
        "accepted": True,
        "device_id": device.serial,
        "tag_count": 3,
        "inserted": 2,
        "skipped": {"xx": "unmapped"},
    }
    assert _values("temperature_weather", zone=zone, user=owner) == [22.5]
    assert _values("humidity_weather", zone=zone, user=owner) == [64.0]
    # Ownership follows the device: every row carries its id.
    from apps.alerts.engine import get_sensor_model

    row = get_sensor_model("temperature_weather").objects.get(zone=zone)
    assert row.device_id == device.id
    assert row.timestamp.isoformat().startswith("2026-11-06T08:00:00")
    assert sorted(c["sensor_key"] for c in dispatched) == [
        "humidity_weather",
        "temperature_weather",
    ]
    assert all(c["device_id"] == device.id for c in dispatched)


def test_mapping_zone_override_routes_to_that_zone(fast, device, zone, owner):
    other_zone = _zone(owner, "Parcelle Sud")
    _map(device, "ta", "temperature_weather", zone_id=other_zone.id)

    assert _uplink(fast, device.serial, {"ta": 19.0}).status_code == 201
    assert _values("temperature_weather", zone=other_zone) == [19.0]
    assert _values("temperature_weather", zone=zone) == []


def test_unknown_device_is_404_and_writes_nothing(fast, zone):
    res = _uplink(fast, "router-user-NOPE", {"ta": 22.5})
    assert res.status_code == 404
    assert res.json() == {
        "detail": "No device registered for device_id 'router-user-NOPE'."
    }
    assert _values("temperature_weather") == []


def test_inactive_device_is_422(fast, device):
    _map(device, "ta", "temperature_weather")
    device.is_active = False
    device.save()

    res = _uplink(fast, device.serial, {"ta": 22.5})
    assert res.status_code == 422
    assert res.json() == {"detail": "device is inactive."}
    assert _values("temperature_weather") == []


def test_nothing_mappable_is_202_with_reasons(fast, owner, device):
    from apps.irrigation.models import Device

    _map(device, "ta", "temperature_weather", is_active=False)  # inactive mapping
    _map(device, "zz", "not_a_sensor_key")
    res = _uplink(fast, device.serial, {"ta": 1.0, "zz": 2.0, "ms": 3.0})
    assert res.status_code == 202, res.text
    assert res.json()["inserted"] == 0
    assert res.json()["skipped"] == {
        "ta": "unmapped",
        "zz": "unknown_sensor_key",
        "ms": "unmapped",
    }

    # No mapping zone and no device zone: nowhere to attribute the reading.
    zoneless = Device.objects.create(
        user=owner, zone=None, device_type="bivocom", serial="router-user-NOZONE"
    )
    _map(zoneless, "ta", "temperature_weather")
    res = _uplink(fast, zoneless.serial, {"ta": 1.0})
    assert res.status_code == 202
    assert res.json()["skipped"] == {"ta": "no_zone"}
    assert _values("temperature_weather") == []


def test_mapping_to_a_zone_no_longer_owned_is_skipped(fast, device, stranger):
    """A mapping made before the device changed hands must not keep feeding
    the previous owner's zone."""
    foreign = _zone(stranger, "Not yours")
    _map(device, "ta", "temperature_weather", zone_id=foreign.id)

    res = _uplink(fast, device.serial, {"ta": 22.5})
    assert res.status_code == 202
    assert res.json()["skipped"] == {"ta": "zone_not_owned"}
    assert _values("temperature_weather") == []


def test_alert_dispatch_failure_does_not_drop_later_tags(
    fast, device, zone, monkeypatch
):
    def _boom(session, **kw):
        raise RuntimeError("alerts down")

    monkeypatch.setattr(ingest, "dispatch_alerts_for_reading", _boom)
    _map(device, "ta", "temperature_weather")
    _map(device, "ha", "humidity_weather")

    res = _uplink(fast, device.serial, {"ta": 22.5, "ha": 64.0})
    assert res.status_code == 201
    assert res.json()["inserted"] == 2
    assert _values("temperature_weather", zone=zone) == [22.5]
    assert _values("humidity_weather", zone=zone) == [64.0]


def test_payload_validation_is_unchanged(fast):
    assert fast.post("/ingest/bivocom", json={"device_id": "x"}).status_code == 422
    assert (
        fast.post(
            "/ingest/bivocom", json={"device_id": "x", "timestamp": TS, "tags": {}}
        ).status_code
        == 422
    )


# ===========================================================================
# Device-sensor CRUD — /devices/{pk}/sensors
# ===========================================================================
def test_sensor_crud_round_trip(fast, admin, device, owner):
    h = _auth(admin)
    base = f"/devices/{device.id}/sensors"
    other_zone = _zone(owner, "Parcelle Est")

    created = fast.post(
        base, json={"tag_name": " ta ", "sensor_key": "temperature_weather"}, headers=h
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["device"] == device.id
    assert body["tag_name"] == "ta"  # trimmed
    assert body["zone"] is None  # inherits the device's zone
    assert body["is_active"] is True
    sid = body["id"]

    listed = fast.get(base, headers=h)
    assert listed.status_code == 200
    assert [s["id"] for s in listed.json()] == [sid]

    patched = fast.patch(
        f"{base}/{sid}",
        json={"zone_id": other_zone.id, "sensor_key": "humidity_weather"},
        headers=h,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["zone"] == other_zone.id
    assert patched.json()["sensor_key"] == "humidity_weather"

    # Explicit null clears the override; omitting the field leaves it alone.
    kept = fast.patch(f"{base}/{sid}", json={"is_active": False}, headers=h)
    assert kept.json()["zone"] == other_zone.id
    assert kept.json()["is_active"] is False
    cleared = fast.patch(f"{base}/{sid}", json={"zone_id": None}, headers=h)
    assert cleared.json()["zone"] is None

    assert fast.delete(f"{base}/{sid}", headers=h).json() == {"status": "deleted"}
    assert fast.get(base, headers=h).json() == []
    assert fast.delete(f"{base}/{sid}", headers=h).status_code == 404


def test_sensor_crud_validation(fast, admin, device, stranger):
    h = _auth(admin)
    base = f"/devices/{device.id}/sensors"

    missing = fast.post(base, json={"tag_name": "ta"}, headers=h)
    assert missing.status_code == 400
    assert missing.json() == {"detail": "tag_name and sensor_key are required."}

    bad_key = fast.post(base, json={"tag_name": "ta", "sensor_key": "nope"}, headers=h)
    assert bad_key.json() == {"detail": "Unknown sensor_key 'nope'."}

    foreign = _zone(stranger, "Not yours")
    bad_zone = fast.post(
        base,
        json={
            "tag_name": "ta",
            "sensor_key": "temperature_weather",
            "zone_id": foreign.id,
        },
        headers=h,
    )
    assert bad_zone.status_code == 400
    assert bad_zone.json() == {"detail": "zone is not owned by the device's owner."}

    ok = fast.post(
        base, json={"tag_name": "ta", "sensor_key": "temperature_weather"}, headers=h
    )
    assert ok.status_code == 201
    dup = fast.post(
        base, json={"tag_name": "ta", "sensor_key": "humidity_weather"}, headers=h
    )
    assert dup.status_code == 400
    assert dup.json() == {"detail": "tag_name 'ta' is already mapped on this device."}

    assert fast.get("/devices/999999/sensors", headers=h).status_code == 404
    assert (
        fast.patch(f"{base}/999999", json={"is_active": False}, headers=h).status_code
        == 404
    )


def test_sensor_crud_is_admin_only(fast, owner, device):
    res = fast.get(f"/devices/{device.id}/sensors", headers=_auth(owner))
    assert res.status_code == 403


def test_deleting_a_device_removes_its_mappings(fast, admin, device):
    _map(device, "ta", "temperature_weather")
    res = fast.delete(f"/devices/{device.id}", headers=_auth(admin))
    assert res.status_code == 200
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM analytics_devicesensor WHERE device_id = %s",
            [device.id],
        )
        assert cursor.fetchone()[0] == 0


def test_mapping_made_via_api_drives_ingest(fast, admin, device, zone):
    """End to end: onboard a tag through the admin API, then ingest it."""
    created = fast.post(
        f"/devices/{device.id}/sensors",
        json={"tag_name": "ms", "sensor_key": "soil_moisture_medium"},
        headers=_auth(admin),
    )
    assert created.status_code == 201
    assert _uplink(fast, device.serial, {"ms": 31.5}).status_code == 201
    assert _values("soil_moisture_medium", zone=zone) == [31.5]
