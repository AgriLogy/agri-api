"""F3 golden parity: /alerts — fastapp must return byte-identical responses to
the Django ninja endpoint it replaces.

Both surfaces drive the SAME committed rows + the SAME Django-minted access
token: Django via DRF's APIClient (ninja mounts at the URL root), fastapp via
Starlette's TestClient. Any drift in shape, ordering, field set, Decimal
rendering, or an error envelope fails the assertion.

Dual-ORM: Postgres only + committed rows (fastapp reads via a separate
SQLAlchemy connection).
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

URL = "/alerts"


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
        username="al-owner",
        email="al-owner@example.com",
        password="irrelevant-3921",
    )


@pytest.fixture
def technician(django_user_model):
    return django_user_model.objects.create_user(
        username="al-tech",
        email="al-tech@example.com",
        password="irrelevant-3921",
        is_technician=True,
    )


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


@pytest.fixture
def zone(owner):
    return _make_zone(owner)


def _make_alert(user, **overrides):
    from apps.alerts.models import Alert

    payload = {
        "name": "hot",
        "type": "Weather Temperature",
        "description": "too hot",
        "condition": ">",
        "condition_nbr": 30,
        "sensor_key": "temperature_weather",
        "is_active": True,
    }
    payload.update(overrides)
    return Alert.objects.create(user=user, **payload)


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both(fast, django, user, path, method="get", **kw):
    tok = _token(user)
    dj = getattr(django, method)(path, HTTP_AUTHORIZATION=f"Bearer {tok}", **kw)
    fk = {}
    if "data" in kw:
        fk["json"] = kw["data"]
    fp = getattr(fast, method)(path, headers={"Authorization": f"Bearer {tok}"}, **fk)
    return dj, fp


# --- reads -----------------------------------------------------------------


def test_list_is_byte_identical(fast, django, owner, zone):
    _make_alert(owner, name="a1")
    _make_alert(owner, name="a2", zone=zone, condition="<", condition_nbr=5)
    dj, fp = _both(fast, django, owner, URL)
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    assert dj.json() == fp.json()
    assert dj.content == fp.content


def test_list_filter_sensor_key_identical(fast, django, owner):
    _make_alert(owner, sensor_key="temperature_weather")
    _make_alert(owner, sensor_key="humidity_weather", type="Humidity")
    dj, fp = _both(fast, django, owner, f"{URL}?sensor_key=humidity_weather")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    assert len(fp.json()) == 1


def test_get_is_byte_identical(fast, django, owner, zone):
    a = _make_alert(owner, zone=zone, grace_override_seconds=60)
    dj, fp = _both(fast, django, owner, f"{URL}/{a.id}")
    assert dj.status_code == fp.status_code == 200
    assert dj.json() == fp.json()
    assert dj.content == fp.content
    # id + computed threshold + Decimal-string condition_nbr are part of the contract
    assert fp.json()["id"] == a.id
    assert fp.json()["threshold"] == 30.0
    assert fp.json()["condition_nbr"] == "30.00"


def test_get_404_is_identical(fast, django, owner):
    dj, fp = _both(fast, django, owner, f"{URL}/99999999")
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


def test_other_users_alert_404(fast, django, owner, technician):
    a = _make_alert(technician, name="foreign")
    dj, fp = _both(fast, django, owner, f"{URL}/{a.id}")
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


# --- create ----------------------------------------------------------------


def _row(alert_id):
    from apps.alerts.models import Alert

    a = Alert.objects.get(id=alert_id)
    return {
        "name": a.name,
        "type": a.type,
        "description": a.description,
        "condition": a.condition,
        "condition_nbr": a.condition_nbr,
        "sensor_key": a.sensor_key,
        "zone_id": a.zone_id,
        "notification_zone_id": a.notification_zone_id,
        "is_active": a.is_active,
        "notify_email": a.notify_email,
        "notify_whatsapp": a.notify_whatsapp,
        "notify_sms": a.notify_sms,
        "grace_override_seconds": a.grace_override_seconds,
        "user_id": a.user_id,
    }


def _owner_alert_ids(owner):
    from apps.alerts.models import Alert

    return set(Alert.objects.filter(user=owner).values_list("id", flat=True))


def test_create_shape_and_row_match(fast, django, owner, zone):
    body = {
        "name": "created",
        "condition": ">",
        "condition_nbr": 42.5,
        "sensor_key": "temperature_weather",
        "type": "Wrong",  # server derives the canonical type from sensor_key
        "zone": zone.id,
    }
    tok = _token(owner)

    seen = _owner_alert_ids(owner)
    dj = django.post(URL, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    assert dj.status_code == 201, dj.content
    id_dj = (_owner_alert_ids(owner) - seen).pop()

    seen = _owner_alert_ids(owner)
    fp = fast.post(URL, json=body, headers={"Authorization": f"Bearer {tok}"})
    assert fp.status_code == 201, fp.text
    id_fp = (_owner_alert_ids(owner) - seen).pop()

    assert set(dj.json()) == set(fp.json())
    assert _row(id_dj) == _row(id_fp)
    # type derivation from the registry
    assert _row(id_fp)["type"] == "Weather Temperature"


def test_create_validation_400_is_identical(fast, django, owner):
    body = {"condition": ">", "condition_nbr": 1}  # missing name
    dj, fp = _both(fast, django, owner, URL, method="post", data=body, format="json")
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_create_unknown_sensor_key_400(fast, django, owner):
    body = {"name": "x", "condition": ">", "condition_nbr": 1, "sensor_key": "nope"}
    dj, fp = _both(fast, django, owner, URL, method="post", data=body, format="json")
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_create_technician_403_is_identical(fast, django, technician):
    body = {"name": "x", "condition": ">", "condition_nbr": 1}
    dj, fp = _both(
        fast, django, technician, URL, method="post", data=body, format="json"
    )
    assert dj.status_code == fp.status_code == 403
    assert dj.content == fp.content


# --- sub-collections -------------------------------------------------------


def test_for_graph_is_byte_identical(fast, django, owner, zone):
    _make_alert(owner, zone=zone)
    _make_alert(owner, name="b", condition="<", condition_nbr=2)
    dj, fp = _both(fast, django, owner, f"{URL}/for-graph")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


def test_for_graph_unknown_sensor_key_400(fast, django, owner):
    dj, fp = _both(fast, django, owner, f"{URL}/for-graph?sensor_key=nope")
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_suggest_missing_sensor_key_400(fast, django, owner):
    dj, fp = _both(fast, django, owner, f"{URL}/suggest")
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_suggest_unknown_strategy_400(fast, django, owner):
    dj, fp = _both(
        fast, django, owner, f"{URL}/suggest?sensor_key=temperature_weather&strategy=x"
    )
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


# --- update / delete -------------------------------------------------------


def test_patch_mutates_identically(fast, django, owner):
    a_dj = _make_alert(owner, name="orig")
    a_fp = _make_alert(owner, name="orig2")
    body = {"condition_nbr": 99, "is_active": False}
    tok = _token(owner)
    dj = django.patch(
        f"{URL}/{a_dj.id}", body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}"
    )
    fp = fast.patch(
        f"{URL}/{a_fp.id}", json=body, headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 200
    assert _row(a_dj.id)["condition_nbr"] == _row(a_fp.id)["condition_nbr"]
    assert _row(a_fp.id)["is_active"] is False
    # response shape identical (volatile updated_at differs, so compare keys)
    assert set(dj.json()) == set(fp.json())


def test_delete_removes_row_204(fast, django, owner):
    from apps.alerts.models import Alert

    a_dj = _make_alert(owner)
    a_fp = _make_alert(owner)
    tok = _token(owner)
    dj = django.delete(f"{URL}/{a_dj.id}", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.delete(f"{URL}/{a_fp.id}", headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == fp.status_code == 204
    assert not Alert.objects.filter(id=a_dj.id).exists()
    assert not Alert.objects.filter(id=a_fp.id).exists()
