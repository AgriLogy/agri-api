"""Device→account attribution routing for ChirpStack uplinks (fastapp ingest).

The fastapp chirpstack ingest path resolves the owning account/zone from the
``analytics_device`` table (``serial`` == DevEUI) via ``resolve_device_zone``
instead of always dumping into the shared ``lora`` catch-all:

  * unknown DevEUI       → auto-registered (unassigned, owned by ``lora``) and
                           routed to the ``lora`` zone,
  * registered+assigned  → routed to the owner's zone,
  * unassigned/inactive  → ``lora`` fallback (no re-registration).

Postgres-only (dual-ORM committed rows), mirroring ``test_ingest_parity``: the
fastapp route writes over a separate SQLAlchemy connection and assertions read
committed rows via the Django ORM, so ``django_db(transaction=True)`` is needed.
Each test uses a unique DevEUI (``lora_uplink`` is unmanaged and never truncated).
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient

from fastapp.main import app

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="device routing requires Postgres (dual-ORM committed rows)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="dev-owner",
        email="dev-owner@example.com",
        password="irrelevant-5521",
    )


@pytest.fixture
def zone(owner):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=owner,
        name="Parcelle Dev",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _uplink(dev_eui: str) -> dict:
    return {
        "deviceInfo": {"devEui": dev_eui, "deviceName": "probe-x"},
        "rxInfo": [{"rssi": -71.0, "snr": 9.2}],
        "txInfo": {"frequency": 868100000},
        "fPort": 2,
        "fCnt": 7,
        "object": {"pH": 6.8, "BatV": 3.5},
    }


def _ph_rows(**filt):
    from apps.alerts.engine import get_sensor_model

    return list(
        get_sensor_model("ph_soil")
        .objects.filter(**filt)
        .values_list("value", flat=True)
    )


def test_unknown_deveui_auto_registers_and_routes_to_lora(fast):
    from apps.irrigation.models import Device, Zone

    dev_eui = "dddddddd00000001"
    r = fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui))
    assert r.status_code == 201, r.text

    # Auto-registered as an unassigned lora device owned by the placeholder.
    dev = Device.objects.get(serial=dev_eui)
    assert dev.device_type == "lora"
    assert dev.zone_id is None
    assert dev.is_active is True
    assert dev.user.username == "lora"

    # Reading landed in the shared lora zone — the only ph_soil row this test.
    lora_zone = Zone.objects.get(name="lora")
    assert _ph_rows() == [6.8]
    assert _ph_rows(zone_id=lora_zone.id) == [6.8]


def test_assigned_device_routes_to_owner_zone(fast, owner, zone):
    from apps.irrigation.models import Device

    dev_eui = "dddddddd00000002"
    Device.objects.create(
        user=owner,
        zone=zone,
        device_type="lora",
        serial=dev_eui,
        name="pH North",
        is_active=True,
    )
    r = fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui))
    assert r.status_code == 201, r.text

    # Routed to the owner's zone (and it is the only ph_soil row).
    assert _ph_rows() == [6.8]
    assert _ph_rows(user_id=owner.id, zone_id=zone.id) == [6.8]


def test_unassigned_registered_device_falls_back_to_lora(fast, owner):
    from apps.irrigation.models import Device, Zone

    dev_eui = "dddddddd00000003"
    Device.objects.create(
        user=owner,
        zone=None,
        device_type="lora",
        serial=dev_eui,
        is_active=True,
    )
    r = fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui))
    assert r.status_code == 201, r.text

    lora_zone = Zone.objects.get(name="lora")
    assert _ph_rows(zone_id=lora_zone.id) == [6.8]
    assert _ph_rows(user_id=owner.id) == []


def test_inactive_device_falls_back_and_is_not_reregistered(fast, owner, zone):
    from apps.irrigation.models import Device, Zone

    dev_eui = "dddddddd00000004"
    Device.objects.create(
        user=owner,
        zone=zone,
        device_type="lora",
        serial=dev_eui,
        is_active=False,
    )
    r = fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui))
    assert r.status_code == 201, r.text

    # Fell back to lora — did NOT route to the owner's (assigned) zone.
    lora_zone = Zone.objects.get(name="lora")
    assert _ph_rows(zone_id=lora_zone.id) == [6.8]
    assert _ph_rows(zone_id=zone.id) == []
    # Existing row untouched — not duplicated, still inactive.
    assert Device.objects.filter(serial=dev_eui).count() == 1
    assert Device.objects.get(serial=dev_eui).is_active is False


def test_auto_register_is_idempotent(fast):
    from apps.irrigation.models import Device

    dev_eui = "dddddddd00000005"
    for _ in range(2):
        resp = fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui))
        assert resp.status_code == 201, resp.text
    assert Device.objects.filter(serial=dev_eui).count() == 1


# --- Phase 1: every device reading is stamped with device_id ----------------
def test_assigned_uplink_stamps_device_id(fast, owner, zone):
    from apps.alerts.engine import get_sensor_model
    from apps.irrigation.models import Device

    dev_eui = "dddddddd000000aa"
    Device.objects.create(
        user=owner, zone=zone, device_type="lora", serial=dev_eui, is_active=True
    )
    assert (
        fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui)).status_code
        == 201
    )

    dev = Device.objects.get(serial=dev_eui)
    ph = get_sensor_model("ph_soil").objects.get(zone_id=zone.id)
    assert ph.device_id == dev.id
    # battery + signal rows carry it too
    assert get_sensor_model("battery").objects.get(zone_id=zone.id).device_id == dev.id
    assert get_sensor_model("signal").objects.get(zone_id=zone.id).device_id == dev.id


def test_unassigned_uplink_still_stamps_device_id(fast):
    """Even when the device is unassigned (reading routes to the lora zone), the
    row is stamped with device_id so it follows the device on assignment."""
    from apps.alerts.engine import get_sensor_model
    from apps.irrigation.models import Device, Zone

    dev_eui = "dddddddd000000bb"
    assert (
        fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui)).status_code
        == 201
    )

    dev = Device.objects.get(serial=dev_eui)  # auto-registered, unassigned
    lora_zone = Zone.objects.get(name="lora")
    ph = get_sensor_model("ph_soil").objects.get(zone_id=lora_zone.id)
    assert ph.device_id == dev.id  # stamped despite the lora fallback
