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


def _uplink_ph(dev_eui, ph):
    u = _uplink(dev_eui)
    u["object"]["pH"] = ph
    return u


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
    # The device's PRIOR (user, zone) is forwarded as the migration source
    # (here: owned by owner, unassigned → source_zone_id None → lora catch-all).
    assert all(
        c[1]["source_user_id"] == owner.id and c[1]["source_zone_id"] is None
        for c in calls
    )


# --- PATCH (single-device edit) backfill ------------------------------------
def test_patch_backfill_enqueues_on_transfer(fast, admin, owner, zone, monkeypatch):
    """Editing a device to a new zone with ``backfill`` migrates its history
    from the prior zone — same task the bulk-assign path uses."""
    from apps.irrigation.models import Zone

    zone_b = Zone.objects.create(
        user=owner,
        name="ba-zone-b",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )
    dev = _mk_device(owner, "PA-1", zone=zone)  # starts in ``zone``
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        celery, "send_task", lambda name, **kw: calls.append((name, kw))
    )

    r = fast.patch(
        f"/devices/{dev.id}",
        headers=_auth(admin),
        json={"username": "ba-owner", "zone_id": zone_b.id, "backfill": True},
    )
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    name, kw = calls[0]
    assert name == "agriapi.tasks.backfill_device_readings"
    assert kw["device_id"] == dev.id
    assert kw["source_user_id"] == owner.id and kw["source_zone_id"] == zone.id
    assert kw["target_user_id"] == owner.id and kw["target_zone_id"] == zone_b.id


def test_patch_no_backfill_without_flag_or_move(fast, admin, owner, zone, monkeypatch):
    dev = _mk_device(owner, "PA-2", zone=zone)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        celery, "send_task", lambda name, **kw: calls.append((name, kw))
    )

    # (a) no backfill flag → no enqueue, even on a rename.
    assert (
        fast.patch(
            f"/devices/{dev.id}", headers=_auth(admin), json={"name": "renamed"}
        ).status_code
        == 200
    )
    # (b) backfill requested but the zone didn't change → no move, no enqueue.
    assert (
        fast.patch(
            f"/devices/{dev.id}",
            headers=_auth(admin),
            json={"zone_id": zone.id, "backfill": True},
        ).status_code
        == 200
    )
    assert calls == []


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

    # Source defaults to the lora catch-all; the device's one reading migrates
    # (mode depends on how many other unassigned devices share lora — the
    # deterministic full/correlated split is covered by the two tests below).
    assert result["moved"]["analytics_phsoil"] == 1
    assert _ph_rows(zone_id=lora_zone.id) == []  # left the lora zone
    assert _ph_rows(user_id=owner.id, zone_id=zone.id) == [6.5]  # arrived at target

    # Idempotent — nothing left to move.
    again = backfill_device_readings(device.id, owner.id, zone.id)
    assert again["moved"]["analytics_phsoil"] == 0


def test_backfill_missing_device_is_noop(owner, zone):
    result = backfill_device_readings(999999, owner.id, zone.id)
    assert result["skipped"] == "device_not_found"


def test_backfill_full_move_from_technician_zone_includes_no_uplink_rows(
    fast, owner, zone, django_user_model
):
    """Tech→client transfer: the source (technician) zone holds only this device,
    so its ENTIRE history moves — including a commissioning reading that has no
    uplink row (which the correlated mode would miss)."""
    from django.utils import timezone

    from apps.alerts.engine import get_sensor_model
    from apps.irrigation.models import Zone

    tech = django_user_model.objects.create_user(
        username="bf-tech", email="bf-tech@x.com", password="pw-1", is_technician=True
    )
    tech_zone = Zone.objects.create(
        user=tech,
        name="bf-tech-zone",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )
    dev_eui = "bf00000000000010"
    dev = _mk_device(tech, dev_eui, zone=tech_zone)

    # (a) a commissioning reading in the tech zone with NO uplink row
    get_sensor_model("ph_soil").objects.create(
        user_id=tech.id, zone_id=tech_zone.id, value=6.11, timestamp=timezone.now()
    )
    # (b) a real uplink while assigned to tech → routes to the tech zone (+uplink)
    assert (
        fast.post(
            "/ingest/lorawan/chirpstack", json=_uplink_ph(dev_eui, 6.22)
        ).status_code
        == 201
    )
    assert sorted(_ph_rows(zone_id=tech_zone.id)) == [6.11, 6.22]

    res = backfill_device_readings(
        dev.id, owner.id, zone.id, source_user_id=tech.id, source_zone_id=tech_zone.id
    )
    assert res["mode"] == "full"
    assert res["moved"]["analytics_phsoil"] == 2
    assert _ph_rows(zone_id=tech_zone.id) == []
    assert sorted(_ph_rows(user_id=owner.id, zone_id=zone.id)) == [6.11, 6.22]


def test_backfill_correlated_when_source_zone_shared(
    fast, owner, zone, django_user_model
):
    """When the source zone holds several devices, only the transferred device's
    (uplink-correlated) readings move — the others stay put."""
    import time

    from apps.irrigation.models import Zone

    shared_user = django_user_model.objects.create_user(
        username="bf-shared", email="bf-shared@x.com", password="pw-1"
    )
    shared_zone = Zone.objects.create(
        user=shared_user,
        name="bf-shared-zone",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )
    d1 = _mk_device(shared_user, "bf00000000000021", zone=shared_zone)
    _mk_device(shared_user, "bf00000000000022", zone=shared_zone)

    assert (
        fast.post(
            "/ingest/lorawan/chirpstack", json=_uplink_ph("bf00000000000021", 6.31)
        ).status_code
        == 201
    )
    time.sleep(2.5)  # keep d2 outside d1's ±2s correlation window
    assert (
        fast.post(
            "/ingest/lorawan/chirpstack", json=_uplink_ph("bf00000000000022", 6.32)
        ).status_code
        == 201
    )
    assert sorted(_ph_rows(zone_id=shared_zone.id)) == [6.31, 6.32]

    res = backfill_device_readings(
        d1.id,
        owner.id,
        zone.id,
        source_user_id=shared_user.id,
        source_zone_id=shared_zone.id,
    )
    assert res["mode"] == "correlated"
    assert res["moved"]["analytics_phsoil"] == 1
    assert _ph_rows(user_id=owner.id, zone_id=zone.id) == [6.31]
    assert _ph_rows(zone_id=shared_zone.id) == [6.32]
