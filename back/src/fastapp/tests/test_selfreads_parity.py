"""F5 golden parity: /users/me + /zones self-reads — fastapp must return
byte-identical responses to the Django ninja endpoints it replaces.

Each test drives BOTH surfaces over the SAME committed test data + the SAME
Django-minted access token: the Django route via DRF's APIClient (ninja is
mounted at the URL root), the fastapp route via Starlette's TestClient. Both
read the same Postgres (AGRI_DB_URL bound to Django's test DB by
back/conftest.py), so any drift in shape, ordering, rounding, or the error
envelope fails the assertion.

Dual-ORM: needs Postgres (skip on sqlite) + committed rows
(``django_db(transaction=True)``) because fastapp reads via a separate
SQLAlchemy connection.
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
    reason="dual-ORM parity requires Postgres (fastapp reads the test DB "
    "Django writes)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


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
        username="sr-owner",
        email="sr-owner@example.com",
        password="irrelevant-3921",
        firstname="Sr",
        lastname="Owner",
        phone_number="+212600000009",
    )


@pytest.fixture
def other(django_user_model):
    return django_user_model.objects.create_user(
        username="sr-other",
        email="sr-other@example.com",
        password="irrelevant-3921",
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
    # Creating a Zone auto-creates its ActiveGraph via the post_save signal.
    return Zone.objects.create(user=user, **payload)


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both_get(fast, django, user, path):
    tok = _token(user)
    dj = django.get(path, HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.get(path, headers={"Authorization": f"Bearer {tok}"})
    return dj, fp


def _both_patch(fast, django, user, path, body):
    tok = _token(user)
    dj = django.patch(path, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.patch(path, json=body, headers={"Authorization": f"Bearer {tok}"})
    return dj, fp


# ---------------------------------------------------------------------------
# GET /users/me
# ---------------------------------------------------------------------------


def test_users_me_get_is_identical(fast, django, owner):
    dj, fp = _both_get(fast, django, owner, "/users/me")
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    assert dj.status_code == fp.status_code
    assert dj.content == fp.content
    body = fp.json()
    assert set(body.keys()) == {
        "username",
        "preferred_language",
        "notify_every",
        "email",
        "phone_number",
        "first_name",
        "last_name",
        "access_level",
    }
    # RBAC tier (#444): exposed for the frontend, byte-identical across surfaces.
    assert body["access_level"] == "editor"
    assert body["username"] == "sr-owner"
    assert body["preferred_language"] == "fr"
    assert body["notify_every"] == 240
    assert body["email"] == "sr-owner@example.com"
    assert body["phone_number"] == "+212600000009"
    assert body["first_name"] == "Sr"
    assert body["last_name"] == "Owner"


# ---------------------------------------------------------------------------
# PATCH /users/me
# ---------------------------------------------------------------------------


def test_users_me_patch_valid_is_identical(fast, django, owner):
    # Both surfaces set the SAME value, so the two responses (and the row they
    # leave behind) are identical.
    dj, fp = _both_patch(fast, django, owner, "/users/me", {"preferred_language": "ar"})
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    assert dj.status_code == fp.status_code
    assert dj.content == fp.content
    assert fp.json()["preferred_language"] == "ar"


def test_users_me_patch_noop_is_identical(fast, django, owner):
    # Empty payload → no language change; returns the current profile.
    dj, fp = _both_patch(fast, django, owner, "/users/me", {})
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    assert fp.json()["preferred_language"] == "fr"


def test_users_me_patch_invalid_language_400_is_identical(fast, django, owner):
    dj, fp = _both_patch(fast, django, owner, "/users/me", {"preferred_language": "zz"})
    assert dj.status_code == 400
    assert fp.status_code == 400
    assert dj.json() == fp.json() == {"preferred_language": "Must be 'fr' or 'ar'."}
    assert dj.content == fp.content  # byte-identical 400 field map


# ---------------------------------------------------------------------------
# PATCH /users/me — profile fields (email / phone / names)
# ---------------------------------------------------------------------------


def test_users_me_patch_profile_updates(fast, django, owner):
    # Drive only fastapp (the live surface) so the single row is mutated once.
    tok = _token(owner)
    body = {
        "email": "sr-owner-new@example.com",
        "phone_number": "+212611111111",
        "first_name": "Newf",
        "last_name": "Newl",
    }
    resp = fast.patch(
        "/users/me", json=body, headers={"Authorization": f"Bearer {tok}"}
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["email"] == "sr-owner-new@example.com"
    assert j["phone_number"] == "+212611111111"
    assert j["first_name"] == "Newf"
    assert j["last_name"] == "Newl"
    # Persisted to the DB (Django ORM reads the same row fastapp wrote).
    owner.refresh_from_db()
    assert owner.email == "sr-owner-new@example.com"
    assert owner.firstname == "Newf"
    assert owner.lastname == "Newl"
    assert owner.phone_number == "+212611111111"


def test_users_me_patch_email_uniqueness_rejected_is_identical(
    fast, django, owner, other
):
    # `other` already owns this address → both surfaces reject with the same
    # 400 field map, and neither writes (safe to drive both).
    body = {"email": "sr-other@example.com"}
    dj, fp = _both_patch(fast, django, owner, "/users/me", body)
    assert dj.status_code == 400
    assert fp.status_code == 400
    assert dj.json() == fp.json() == {"email": "This email is already in use."}
    assert dj.content == fp.content
    owner.refresh_from_db()
    assert owner.email == "sr-owner@example.com"  # unchanged


def test_users_me_patch_invalid_email_rejected_is_identical(fast, django, owner):
    dj, fp = _both_patch(fast, django, owner, "/users/me", {"email": "not-an-email"})
    assert dj.status_code == 400
    assert fp.status_code == 400
    assert dj.json() == fp.json() == {"email": "Enter a valid email address."}
    assert dj.content == fp.content


# ---------------------------------------------------------------------------
# POST /users/me/change-password
# ---------------------------------------------------------------------------


def test_change_password_happy_path(fast, django, owner):
    tok = _token(owner)
    resp = fast.post(
        "/users/me/change-password",
        json={"current_password": "irrelevant-3921", "new_password": "Brand-New-Pw-1"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": "Password updated."}
    # New hash is Django-compatible: check_password verifies it.
    owner.refresh_from_db()
    assert owner.check_password("Brand-New-Pw-1")
    assert not owner.check_password("irrelevant-3921")


def test_change_password_wrong_current_rejected_is_identical(fast, django, owner):
    body = {"current_password": "totally-wrong", "new_password": "Brand-New-Pw-1"}
    tok = _token(owner)
    dj = django.post(
        "/users/me/change-password",
        body,
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        "/users/me/change-password",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == 400
    assert fp.status_code == 400
    assert (
        dj.json() == fp.json() == {"current_password": "Current password is incorrect."}
    )
    assert dj.content == fp.content
    # No mutation on rejection.
    owner.refresh_from_db()
    assert owner.check_password("irrelevant-3921")


def test_change_password_too_short_rejected_is_identical(fast, django, owner):
    body = {"current_password": "irrelevant-3921", "new_password": "Ab2!x"}
    tok = _token(owner)
    dj = django.post(
        "/users/me/change-password",
        body,
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        "/users/me/change-password",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == 400
    assert fp.status_code == 400
    assert dj.json() == fp.json()
    assert fp.json() == {
        "new_password": [
            "This password is too short. It must contain at least 8 characters."
        ]
    }
    assert dj.content == fp.content
    owner.refresh_from_db()
    assert owner.check_password("irrelevant-3921")


# ---------------------------------------------------------------------------
# GET /zones
# ---------------------------------------------------------------------------


def test_zones_list_is_identical(fast, django, owner, other):
    _make_zone(owner, name="Alpha")
    _make_zone(owner, name="Beta")
    _make_zone(other, name="Foreign")  # must NOT leak into owner's list
    dj, fp = _both_get(fast, django, owner, "/zones")
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    dj_body, fp_body = dj.json(), fp.json()
    # /zones is cut over to fastapp, which now extends the legacy Django shape
    # with sector_id/sector_name (the sector feature). Zones here are
    # unassigned → both None; every other field stays parity with Django.
    for z in fp_body:
        assert z["sector_id"] is None and z["sector_name"] is None
    assert [
        {k: v for k, v in z.items() if k not in ("sector_id", "sector_name")}
        for z in fp_body
    ] == dj_body
    assert {z["name"] for z in fp_body} == {"Alpha", "Beta"}


def test_zones_list_empty_is_identical(fast, django, owner):
    dj, fp = _both_get(fast, django, owner, "/zones")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    assert fp.json() == []


# ---------------------------------------------------------------------------
# GET /zones/{id}/active-graph
# ---------------------------------------------------------------------------


def test_active_graph_is_identical(fast, django, owner):
    zone = _make_zone(owner)
    dj, fp = _both_get(fast, django, owner, f"/zones/{zone.id}/active-graph")
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    assert dj.content == fp.content
    body = fp.json()
    # FK ids under their field names, then the status booleans; no `id`.
    assert body["user"] == owner.id
    assert body["zone"] == zone.id
    assert "id" not in body
    assert body["soil_irrigation_status"] is True


def test_active_graph_missing_404_is_identical(fast, django, owner):
    dj, fp = _both_get(fast, django, owner, "/zones/99999999/active-graph")
    assert dj.status_code == 404
    assert fp.status_code == 404
    assert dj.json() == fp.json() == {"detail": "ActiveGraph not found."}
    assert dj.content == fp.content


def test_active_graph_foreign_zone_404_is_identical(fast, django, owner, other):
    foreign = _make_zone(other, name="Autre")
    dj, fp = _both_get(fast, django, owner, f"/zones/{foreign.id}/active-graph")
    assert dj.status_code == 404
    assert fp.status_code == 404
    assert dj.json() == fp.json() == {"detail": "ActiveGraph not found."}
    assert dj.content == fp.content


def test_missing_auth_is_401_on_both(fast, django):
    dj = django.get("/users/me")
    fp = fast.get("/users/me")
    assert dj.status_code == 401
    assert fp.status_code == 401


# ---------------------------------------------------------------------------
# GET /my-devices  (fastapp-native; farmer-scoped device list for the map)
# ---------------------------------------------------------------------------


def test_my_devices_returns_owner_active_devices_with_coords(fast, owner, other):
    from apps.irrigation.models import Device

    Device.objects.create(
        user=owner,
        device_type="lora",
        serial="MD-1",
        name="Sensor A",
        is_active=True,
        latitude=33.5731,
        longitude=-7.5898,
    )
    Device.objects.create(  # owner's device without coordinates
        user=owner,
        device_type="lora",
        serial="MD-2",
        name="Sensor B",
        is_active=True,
    )
    Device.objects.create(  # inactive -> excluded
        user=owner,
        device_type="lora",
        serial="MD-OFF",
        name="Off",
        is_active=False,
        latitude=1.0,
        longitude=2.0,
    )
    Device.objects.create(  # another user's device -> excluded
        user=other,
        device_type="lora",
        serial="MD-OTHER",
        name="Theirs",
        is_active=True,
        latitude=10.0,
        longitude=20.0,
    )

    resp = fast.get("/my-devices", headers={"Authorization": f"Bearer {_token(owner)}"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    by_serial = {d["serial"]: d for d in data}
    assert set(by_serial) == {"MD-1", "MD-2"}  # only the owner's active devices
    assert by_serial["MD-1"]["latitude"] == 33.5731
    assert by_serial["MD-1"]["longitude"] == -7.5898
    assert by_serial["MD-2"]["latitude"] is None
    assert by_serial["MD-2"]["longitude"] is None
    assert set(by_serial["MD-1"]) == {
        "id",
        "device_type",
        "serial",
        "name",
        "zone",
        "latitude",
        "longitude",
    }


def test_my_devices_requires_auth(fast):
    assert fast.get("/my-devices").status_code == 401
