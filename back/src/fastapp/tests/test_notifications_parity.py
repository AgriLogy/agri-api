"""F3 golden parity: /notifications + /notification-zones — fastapp must return
byte-identical responses to the Django ninja endpoints it replaces.

Each read/error test drives BOTH surfaces over the SAME committed rows + the
SAME Django-minted access token (Django ninja at the URL root via DRF's
APIClient; fastapp via Starlette's TestClient) and asserts identical status +
bytes. Writes (create / patch) can't be byte-compared (auto ids + timestamps
differ per insert), so they assert identical status + the stable response
shape. The Celery-enqueue route (zone-outbound) monkeypatches the fastapp
enqueue helper to a no-op and asserts the 202/400 contract directly.

Dual-ORM: needs Postgres (skip on sqlite) + committed rows
(``django_db(transaction=True)``) because fastapp reads/writes over a separate
SQLAlchemy connection.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp import celery
from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres (fastapp reads the test DB "
    "Django writes)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def django() -> APIClient:
    return APIClient()


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="nz-owner",
        email="nz-owner@example.com",
        password="irrelevant-3921",
        phone_number="+212600000000",
    )


@pytest.fixture
def other(django_user_model):
    return django_user_model.objects.create_user(
        username="nz-other",
        email="nz-other@example.com",
        password="irrelevant-3921",
    )


@pytest.fixture
def technician(django_user_model):
    return django_user_model.objects.create_user(
        username="nz-tech",
        email="nz-tech@example.com",
        password="irrelevant-3921",
        is_technician=True,
    )


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both_get(fast, django, user, path):
    tok = _token(user)
    dj = django.get(path, HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.get(path, headers={"Authorization": f"Bearer {tok}"})
    return dj, fp


def _make_zone(user, **overrides):
    from apps.irrigation.models import Zone

    payload = {
        "name": f"zone-{user.username}",
        "space": 1000.0,
        "critical_moisture_threshold": 20.0,
        "pomp_flow_rate": 1.0,
        "elevation_m": 120.0,
    }
    payload.update(overrides)
    return Zone.objects.create(user=user, **payload)


# ---------------------------------------------------------------------------
# GET /notifications  (feed)
# ---------------------------------------------------------------------------


def _seed_notification(user, *, date: datetime.datetime):
    from apps.alerts.models import Notification

    return Notification.objects.create(
        user=user,
        yesterday_temperature=Decimal("12.30"),
        today_temperature=Decimal("14.50"),
        yesterday_humidity=Decimal("60.00"),
        today_humidity=Decimal("62.25"),
        ET0=Decimal("4.20"),
        soil_humidity=Decimal("30.10"),
        soil_temperature=Decimal("18.00"),
        soil_ph=Decimal("6.50"),
        perfect_irrigation_period="06:00-08:00",
        last_irrigation_date=datetime.date(2026, 6, 30),
        last_start_irrigation_hour=datetime.time(6, 0, 0),
        last_finish_irrigation_hour=datetime.time(7, 30, 0),
        used_water_irrigation=Decimal("125.00"),
        notification_date=date,
    )


def test_feed_is_byte_identical(fast, django, owner):
    # Distinct notification_date so the "-notification_date" ordering is
    # deterministic across both ORMs.
    _seed_notification(owner, date=datetime.datetime(2026, 6, 30, 10, 0, tzinfo=UTC))
    _seed_notification(owner, date=datetime.datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    dj, fp = _both_get(fast, django, owner, "/notifications")
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    assert dj.content == fp.content
    body = fp.json()
    assert len(body["notifications"]) == 2
    # newest first
    assert body["notifications"][0]["notification"]["notification_date"].startswith(
        "2026-07-01"
    )


def test_feed_empty_is_byte_identical(fast, django, other):
    dj, fp = _both_get(fast, django, other, "/notifications")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content == b'{"notifications": []}'


def test_feed_missing_auth_is_401_on_both(fast, django):
    dj = django.get("/notifications")
    fp = fast.get("/notifications")
    assert dj.status_code == fp.status_code == 401


# ---------------------------------------------------------------------------
# GET /notification-zones + /{pk} + available-sensors
# ---------------------------------------------------------------------------


def _seed_zone(
    user, *, name="Zone A", source_zone=None, sensor_key="temperature_weather"
):
    from apps.alerts.models import NotificationZone, NotificationZoneSensor

    nz = NotificationZone.objects.create(
        user=user, name=name, description="desc", is_active=True
    )
    NotificationZoneSensor.objects.create(
        notification_zone=nz,
        sensor_key=sensor_key,
        source_zone=source_zone,
        label=None,
    )
    return nz


def test_list_zones_is_byte_identical(fast, django, owner):
    farm = _make_zone(owner)
    _seed_zone(owner, name="Zone A", source_zone=farm)
    _seed_zone(owner, name="Zone B")
    dj, fp = _both_get(fast, django, owner, "/notification-zones")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


def test_list_zones_empty_is_byte_identical(fast, django, other):
    dj, fp = _both_get(fast, django, other, "/notification-zones")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content == b"[]"


def test_get_zone_is_byte_identical(fast, django, owner):
    farm = _make_zone(owner)
    nz = _seed_zone(owner, source_zone=farm)
    dj, fp = _both_get(fast, django, owner, f"/notification-zones/{nz.id}")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


def test_get_zone_missing_404_is_byte_identical(fast, django, owner):
    dj, fp = _both_get(fast, django, owner, "/notification-zones/99999999")
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content == b'{"detail": "Notification zone not found."}'


def test_get_other_users_zone_404_is_byte_identical(fast, django, owner, other):
    foreign = _seed_zone(other, name="Foreign")
    dj, fp = _both_get(fast, django, owner, f"/notification-zones/{foreign.id}")
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


def test_available_sensors_is_byte_identical(fast, django, owner):
    from apps.sensors.models import TemperatureWeather

    farm = _make_zone(owner)
    TemperatureWeather.objects.create(
        user=owner,
        zone=farm,
        value=21.5,
        timestamp=datetime.datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
    )
    dj, fp = _both_get(fast, django, owner, "/notification-zones/available-sensors")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    body = fp.json()
    keys = [s["sensor_key"] for s in body["zones"][0]["sensors"]]
    assert "temperature_weather" in keys


def test_available_sensors_empty_is_byte_identical(fast, django, other):
    dj, fp = _both_get(fast, django, other, "/notification-zones/available-sensors")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content == b'{"zones": []}'


# ---------------------------------------------------------------------------
# Writes: create / patch / delete / add-sensor / remove-sensor
# ---------------------------------------------------------------------------


def _strip(zone: dict) -> dict:
    """Drop the fields that differ per-insert (id + timestamps) so two
    independent creates can be compared on their stable shape."""
    out = {k: v for k, v in zone.items() if k not in ("id", "created_at", "updated_at")}
    out["sensors"] = [
        {k: v for k, v in s.items() if k != "id"} for s in zone.get("sensors", [])
    ]
    return out


def _post(client_pair, user, path, body):
    tok = _token(user)
    fast, django = client_pair
    dj = django.post(path, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.post(path, json=body, headers={"Authorization": f"Bearer {tok}"})
    return dj, fp


def test_create_zone_shape_matches(fast, django, owner):
    farm = _make_zone(owner)
    body = {
        "name": "New Zone",
        "description": "hello",
        "is_active": False,
        "sensors": [{"sensor_key": "temperature_weather", "source_zone": farm.id}],
    }
    dj, fp = _post((fast, django), owner, "/notification-zones", body)
    assert dj.status_code == fp.status_code == 201, (dj.content, fp.text)
    assert _strip(dj.json()) == _strip(fp.json())
    # both timestamps populated (auto_now_add mirror)
    assert fp.json()["created_at"] and fp.json()["updated_at"]


def test_create_zone_name_required_400_is_byte_identical(fast, django, owner):
    dj, fp = _post((fast, django), owner, "/notification-zones", {"description": "x"})
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content == b'{"detail": "name is required."}'


def test_create_zone_unknown_sensor_400_is_byte_identical(fast, django, owner):
    body = {"name": "Z", "sensors": [{"sensor_key": "bogus_key"}]}
    dj, fp = _post((fast, django), owner, "/notification-zones", body)
    assert dj.status_code == fp.status_code == 400
    assert (
        dj.content == fp.content == b'{"detail": "Unknown sensor_key \'bogus_key\'."}'
    )


def test_create_zone_bad_source_zone_400_is_byte_identical(fast, django, owner):
    body = {
        "name": "Z",
        "sensors": [{"sensor_key": "temperature_weather", "source_zone": 987654321}],
    }
    dj, fp = _post((fast, django), owner, "/notification-zones", body)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_patch_zone_shape_matches(fast, django, owner):
    dj_nz = _seed_zone(owner, name="Dj Patch")
    fp_nz = _seed_zone(owner, name="Fp Patch")
    tok = _token(owner)
    body = {"name": "Renamed", "is_active": False, "sensors": []}
    dj = django.patch(
        f"/notification-zones/{dj_nz.id}",
        body,
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.patch(
        f"/notification-zones/{fp_nz.id}",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    assert _strip(dj.json()) == _strip(fp.json())
    assert fp.json()["name"] == "Renamed"
    assert fp.json()["sensors"] == []


def test_delete_zone_is_byte_identical(fast, django, owner):
    dj_nz = _seed_zone(owner)
    fp_nz = _seed_zone(owner)
    tok = _token(owner)
    dj = django.delete(
        f"/notification-zones/{dj_nz.id}", HTTP_AUTHORIZATION=f"Bearer {tok}"
    )
    fp = fast.delete(
        f"/notification-zones/{fp_nz.id}", headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 200
    # Same shape ({"deleted": <pk>}); pk differs, so compare the key set.
    assert set(dj.json()) == set(fp.json()) == {"deleted"}
    assert dj.json()["deleted"] == dj_nz.id
    assert fp.json()["deleted"] == fp_nz.id


def test_delete_zone_missing_404_is_byte_identical(fast, django, owner):
    tok = _token(owner)
    dj = django.delete(
        "/notification-zones/99999999", HTTP_AUTHORIZATION=f"Bearer {tok}"
    )
    fp = fast.delete(
        "/notification-zones/99999999", headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


def test_add_sensor_shape_matches(fast, django, owner):
    dj_nz = _seed_zone(owner, name="DjAdd")
    fp_nz = _seed_zone(owner, name="FpAdd")
    tok = _token(owner)
    body = {"sensor_key": "humidity_weather"}
    dj = django.post(
        f"/notification-zones/{dj_nz.id}/sensors",
        body,
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        f"/notification-zones/{fp_nz.id}/sensors",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 201, (dj.content, fp.text)
    assert {k: v for k, v in dj.json().items() if k != "id"} == {
        k: v for k, v in fp.json().items() if k != "id"
    }


def test_remove_sensor_missing_404_is_byte_identical(fast, django, owner):
    nz = _seed_zone(owner)
    tok = _token(owner)
    path = f"/notification-zones/{nz.id}/sensors/99999999"
    dj = django.delete(path, HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.delete(path, headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content == b'{"detail": "Sensor assignment not found."}'


def test_technician_write_403_is_byte_identical(fast, django, technician):
    tok = _token(technician)
    body = {"name": "Z"}
    dj = django.post(
        "/notification-zones", body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}"
    )
    fp = fast.post(
        "/notification-zones", json=body, headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 403
    assert dj.content == fp.content == b'{"detail": "Read-only (technician) access."}'


# ---------------------------------------------------------------------------
# POST /notifications/zone-outbound  (Celery-enqueue)
# ---------------------------------------------------------------------------


@pytest.fixture
def _capture_enqueue(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def _fake(name, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(celery, "send_task", _fake)
    return calls


def _outbound(fast, owner, body):
    tok = _token(owner)
    return fast.post(
        "/notifications/zone-outbound",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )


def test_zone_outbound_noop(fast, owner, _capture_enqueue):
    fp = _outbound(fast, owner, {"channels": {}})
    assert fp.status_code == 202
    assert fp.content == b'{"status": "noop"}'
    assert _capture_enqueue == []


def test_zone_outbound_queued_email(fast, owner, _capture_enqueue):
    fp = _outbound(fast, owner, {"channels": {"email": True}})
    assert fp.status_code == 202
    assert fp.json() == {"status": "queued", "channels": ["email"]}
    assert len(_capture_enqueue) == 1
    name, kwargs = _capture_enqueue[0]
    assert name == "agriapi.tasks.send_zone_outbound_email"
    assert kwargs["recipient"] == "nz-owner@example.com"
    assert kwargs["subject"] and kwargs["message"]


def test_zone_outbound_queued_sms_and_whatsapp(fast, owner, _capture_enqueue):
    fp = _outbound(
        fast, owner, {"channels": {"sms": True, "whatsapp": True}, "message": "hi"}
    )
    assert fp.status_code == 202
    assert fp.json() == {"status": "queued", "channels": ["sms", "whatsapp"]}
    names = [c[0] for c in _capture_enqueue]
    assert names == [
        "agriapi.tasks.send_zone_outbound_sms",
        "agriapi.tasks.send_zone_outbound_whatsapp",
    ]
    for _, kwargs in _capture_enqueue:
        assert kwargs["to_phone"] == "+212600000000"
        assert kwargs["body"] == "hi"


def test_zone_outbound_no_recipient_400(fast, django_user_model, _capture_enqueue):
    # A user with no phone number, asking for SMS with no contactPhone → 400
    # (email is required by the manager, but the SMS channel never uses it).
    u = django_user_model.objects.create_user(
        username="nz-nophone",
        email="nz-nophone@example.com",
        password="irrelevant-3921",
    )
    tok = _token(u)
    fp = fast.post(
        "/notifications/zone-outbound",
        json={"channels": {"sms": True}},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert fp.status_code == 400
    assert fp.content == b'{"detail": "no usable recipient for the selected channels"}'
    assert _capture_enqueue == []


def test_zone_outbound_missing_auth_401(fast):
    fp = fast.post("/notifications/zone-outbound", json={"channels": {"email": True}})
    assert fp.status_code == 401
