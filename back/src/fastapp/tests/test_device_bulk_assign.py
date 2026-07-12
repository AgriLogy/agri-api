"""Bulk device→account attribution + historical backfill.

Covers ``POST /devices/bulk-assign`` (multi-select assignment used by the admin
console) and the ``backfill_device_readings`` task it enqueues, which migrates a
device's past readings out of the shared ``lora`` catch-all into its new zone.

Postgres-only (dual-ORM committed rows), mirroring ``test_devices_parity``.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp import celery
from fastapp.main import app
from fastapp.settings import get_settings
from fastapp.tasks_devices import backfill_device_readings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(django_user_model):
    return django_user_model.objects.create_user(
        username="ba-admin", email="ba-admin@x.com", password="pw-1", is_staff=True
    )


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="ba-owner", email="ba-owner@x.com", password="pw-1"
    )


@pytest.fixture
def zone(owner):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=owner,
        name="ba-zone",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _auth(user):
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


def _mk_device(user, serial, **over):
    from apps.irrigation.models import Device

    return Device.objects.create(
        user=user, device_type="lora", serial=serial, name="n", **over
    )


def _ph_rows(**filt):
    from apps.alerts.engine import get_sensor_model

    return list(
        get_sensor_model("ph_soil")
        .objects.filter(**filt)
        .values_list("value", flat=True)
    )


def _uplink(dev_eui):
    return {
        "deviceInfo": {"devEui": dev_eui, "deviceName": "p"},
        "rxInfo": [{"rssi": -70.0, "snr": 9.0}],
        "txInfo": {"frequency": 868100000},
        "fPort": 2,
        "fCnt": 3,
        "object": {"pH": 6.5, "BatV": 3.5},
    }


# --- bulk-assign endpoint ---------------------------------------------------
def test_bulk_assign_routes_devices(fast, admin, owner, zone):
    from apps.irrigation.models import Device

    d1 = _mk_device(owner, "BA-1")
    d2 = _mk_device(owner, "BA-2")
    r = fast.post(
        "/devices/bulk-assign",
        headers=_auth(admin),
        json={"device_ids": [d1.id, d2.id], "username": "ba-owner", "zone_id": zone.id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["assigned"]) == sorted([d1.id, d2.id])
    assert body["failed"] == []
    assert body["backfill_enqueued"] is False
    for d in (d1, d2):
        d.refresh_from_db()
        assert d.zone_id == zone.id and d.user_id == owner.id
    assert Device.objects.filter(zone_id=zone.id).count() == 2


def test_bulk_assign_partial_failure(fast, admin, owner, zone):
    d1 = _mk_device(owner, "BA-OK")
    r = fast.post(
        "/devices/bulk-assign",
        headers=_auth(admin),
        json={
            "device_ids": [d1.id, 999999],
            "username": "ba-owner",
            "zone_id": zone.id,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assigned"] == [d1.id]
    assert body["failed"] == [{"id": 999999, "reason": "device not found."}]


def test_bulk_assign_requires_admin(fast, owner, zone):
    d1 = _mk_device(owner, "BA-403")
    r = fast.post(
        "/devices/bulk-assign",
        headers=_auth(owner),
        json={"device_ids": [d1.id], "username": "ba-owner", "zone_id": zone.id},
    )
    assert r.status_code == 403, r.text


def test_bulk_assign_validates_input(fast, admin, owner, zone):
    # missing device_ids
    assert (
        fast.post(
            "/devices/bulk-assign",
            headers=_auth(admin),
            json={"device_ids": [], "username": "ba-owner", "zone_id": zone.id},
        ).status_code
        == 400
    )
    # missing zone_id
    d1 = _mk_device(owner, "BA-V")
    assert (
        fast.post(
            "/devices/bulk-assign",
            headers=_auth(admin),
            json={"device_ids": [d1.id], "username": "ba-owner"},
        ).status_code
        == 400
    )


def test_bulk_assign_zone_not_owned(fast, admin, owner, zone, django_user_model):
    other = django_user_model.objects.create_user(
        username="ba-other", email="ba-other@x.com", password="pw-1"
    )
    d1 = _mk_device(owner, "BA-OWN")
    # zone belongs to ``owner``, but we target ``other`` → 400
    r = fast.post(
        "/devices/bulk-assign",
        headers=_auth(admin),
        json={"device_ids": [d1.id], "username": "ba-other", "zone_id": zone.id},
    )
    assert r.status_code == 400, r.text
    assert other.id  # silence lint: fixture used for the not-owned case


def test_bulk_assign_backfill_enqueues(fast, admin, owner, zone, monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        celery, "send_task", lambda name, **kw: calls.append((name, kw))
    )
    d1 = _mk_device(owner, "BA-BF1")
    d2 = _mk_device(owner, "BA-BF2")
    r = fast.post(
        "/devices/bulk-assign",
        headers=_auth(admin),
        json={
            "device_ids": [d1.id, d2.id],
            "username": "ba-owner",
            "zone_id": zone.id,
            "backfill": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["backfill_enqueued"] is True
    assert {c[1]["device_id"] for c in calls} == {d1.id, d2.id}
    assert all(c[0] == "agriapi.tasks.backfill_device_readings" for c in calls)
    assert all(
        c[1]["target_user_id"] == owner.id and c[1]["target_zone_id"] == zone.id
        for c in calls
    )


# --- backfill task ----------------------------------------------------------
def test_backfill_moves_lora_readings_to_target(fast, owner, zone):
    from apps.irrigation.models import Device, Zone

    dev_eui = "bf00000000000001"
    # Ingest one uplink → lora_uplink row + readings under the auto-created
    # lora zone, and the device auto-registers (unassigned).
    assert (
        fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui)).status_code
        == 201
    )
    lora_zone = Zone.objects.get(name="lora")
    assert _ph_rows(zone_id=lora_zone.id) == [6.5]

    device = Device.objects.get(serial=dev_eui)
    result = backfill_device_readings(device.id, owner.id, zone.id)

    assert result["moved"]["analytics_phsoil"] == 1
    assert _ph_rows(zone_id=lora_zone.id) == []  # left the lora zone
    assert _ph_rows(user_id=owner.id, zone_id=zone.id) == [6.5]  # arrived at target

    # Idempotent — nothing left to move.
    again = backfill_device_readings(device.id, owner.id, zone.id)
    assert again["moved"]["analytics_phsoil"] == 0


def test_backfill_missing_device_is_noop(owner, zone):
    result = backfill_device_readings(999999, owner.id, zone.id)
    assert result["skipped"] == "device_not_found"
