"""Bulk device→account attribution (``POST /devices/bulk-assign``).

A transfer is now just an ``analytics_device`` UPDATE — readings resolve
ownership via the device JOIN, so the device's history follows automatically
(no backfill). The legacy ``backfill`` flag is accepted but ignored.

Postgres-only (dual-ORM committed rows), mirroring ``test_devices_parity``.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.settings import get_settings

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
    assert (
        fast.post(
            "/devices/bulk-assign",
            headers=_auth(admin),
            json={"device_ids": [], "username": "ba-owner", "zone_id": zone.id},
        ).status_code
        == 400
    )
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
    django_user_model.objects.create_user(
        username="ba-other", email="ba-other@x.com", password="pw-1"
    )
    d1 = _mk_device(owner, "BA-OWN")
    r = fast.post(
        "/devices/bulk-assign",
        headers=_auth(admin),
        json={"device_ids": [d1.id], "username": "ba-other", "zone_id": zone.id},
    )
    assert r.status_code == 400, r.text


def test_bulk_assign_ignores_legacy_backfill_flag(fast, admin, owner, zone):
    """The deprecated ``backfill`` flag is accepted (older clients) but ignored —
    history follows the device via the JOIN, so there's nothing to migrate."""
    d1 = _mk_device(owner, "BA-BF")
    r = fast.post(
        "/devices/bulk-assign",
        headers=_auth(admin),
        json={
            "device_ids": [d1.id],
            "username": "ba-owner",
            "zone_id": zone.id,
            "backfill": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["assigned"] == [d1.id]


# --- PATCH (single-device edit) transfer ------------------------------------
def test_patch_owner_zone_change(fast, admin, owner, zone, django_user_model):
    """Editing a device's owner/zone just updates analytics_device — no
    migration needed (readings follow via the device JOIN)."""
    from apps.irrigation.models import Zone

    prev = django_user_model.objects.create_user(
        username="ba-prev", email="ba-prev@x.com", password="pw-1"
    )
    prev_zone = Zone.objects.create(
        user=prev,
        name="ba-prev-zone",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )
    dev = _mk_device(prev, "PA-1", zone=prev_zone)
    r = fast.patch(
        f"/devices/{dev.id}",
        headers=_auth(admin),
        json={"username": "ba-owner", "zone_id": zone.id, "backfill": True},
    )
    assert r.status_code == 200, r.text
    dev.refresh_from_db()
    assert dev.user_id == owner.id and dev.zone_id == zone.id
