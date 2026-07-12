"""Phase 3: device-keyed read resolution.

The sensor read query resolves ownership via a JOIN to ``analytics_device``
(``COALESCE(device.owner, row.owner)``). So transferring a device to another
account/zone — a single-row UPDATE on ``analytics_device`` — instantly moves the
device's whole graph history to the new owner, with NO reading rewrite: the
reading rows' ``user_id``/``zone_id`` snapshot is left untouched.

Postgres-only (dual-ORM committed rows), mirroring ``test_ingest_parity``.
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
    reason="device read resolution requires Postgres (dual-ORM committed rows)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


def _mk_user_zone(django_user_model, tag):
    from apps.irrigation.models import Zone

    user = django_user_model.objects.create_user(
        username=f"rr-{tag}", email=f"rr-{tag}@x.com", password="pw-1"
    )
    zone = Zone.objects.create(
        user=user,
        name=f"rr-{tag}-zone",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )
    return user, zone


def _auth(user):
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


def _uplink(dev_eui, ph):
    return {
        "deviceInfo": {"devEui": dev_eui, "deviceName": "p"},
        "rxInfo": [{"rssi": -70.0, "snr": 9.0}],
        "txInfo": {"frequency": 868100000},
        "fPort": 2,
        "fCnt": 1,
        "object": {"pH": ph, "BatV": 3.5},
    }


def _ph_graph(fast, user, zone_id):
    r = fast.get(f"/sensors/phsoil?zone={zone_id}", headers=_auth(user))
    assert r.status_code == 200, r.text
    return [row["value"] for row in r.json()]


def test_transfer_moves_graph_with_no_reading_rewrite(fast, django_user_model):
    from apps.irrigation.models import Device

    tech, tech_zone = _mk_user_zone(django_user_model, "tech")
    client, client_zone = _mk_user_zone(django_user_model, "client")

    dev_eui = "eeee000000000001"
    dev = Device.objects.create(
        user=tech, zone=tech_zone, device_type="lora", serial=dev_eui, is_active=True
    )
    # Commission under the technician: reading is stamped with device_id.
    assert (
        fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui, 6.5)).status_code
        == 201
    )
    from apps.alerts.engine import get_sensor_model

    reading = get_sensor_model("ph_soil").objects.get(zone_id=tech_zone.id)
    assert reading.device_id == dev.id

    # Graph resolves under the technician; the client sees nothing yet.
    assert _ph_graph(fast, tech, tech_zone.id) == [6.5]
    assert _ph_graph(fast, client, client_zone.id) == []

    # TRANSFER — a single-row update on analytics_device, no reading touched.
    Device.objects.filter(id=dev.id).update(user=client, zone=client_zone)

    # The whole history instantly follows the device to the client...
    assert _ph_graph(fast, client, client_zone.id) == [6.5]
    # ...and is gone from the technician.
    assert _ph_graph(fast, tech, tech_zone.id) == []

    # The reading ROW was never rewritten — its snapshot still points at tech.
    reading.refresh_from_db()
    assert reading.user_id == tech.id and reading.zone_id == tech_zone.id
    assert reading.device_id == dev.id


def test_raw_readings_also_follow_the_device(fast, django_user_model):
    from apps.irrigation.models import Device

    tech, tech_zone = _mk_user_zone(django_user_model, "rtech")
    client, client_zone = _mk_user_zone(django_user_model, "rclient")
    dev_eui = "eeee000000000002"
    dev = Device.objects.create(
        user=tech, zone=tech_zone, device_type="lora", serial=dev_eui, is_active=True
    )
    assert (
        fast.post("/ingest/lorawan/chirpstack", json=_uplink(dev_eui, 7.1)).status_code
        == 201
    )
    Device.objects.filter(id=dev.id).update(user=client, zone=client_zone)

    r = fast.get(
        f"/sensors/phsoil?zone={client_zone.id}&raw=true", headers=_auth(client)
    )
    assert r.status_code == 200, r.text
    assert [row["value"] for row in r.json()] == [7.1]


def test_non_device_reading_unaffected(fast, django_user_model):
    """A reading with no device_id (weather/manual) resolves by its own row."""
    from apps.alerts.engine import get_sensor_model
    from django.utils import timezone

    owner, zone = _mk_user_zone(django_user_model, "weather")
    get_sensor_model("humidity_weather").objects.create(
        user_id=owner.id, zone_id=zone.id, value=55.0, timestamp=timezone.now()
    )
    r = fast.get(f"/sensors/humidityweather?zone={zone.id}", headers=_auth(owner))
    assert r.status_code == 200, r.text
    assert [row["value"] for row in r.json()] == [55.0]
