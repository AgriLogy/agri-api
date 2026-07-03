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
    assert set(body.keys()) == {"username", "preferred_language", "notify_every"}
    assert body["username"] == "sr-owner"
    assert body["preferred_language"] == "fr"
    assert body["notify_every"] == 240


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
# GET /zones
# ---------------------------------------------------------------------------


def test_zones_list_is_identical(fast, django, owner, other):
    _make_zone(owner, name="Alpha")
    _make_zone(owner, name="Beta")
    _make_zone(other, name="Foreign")  # must NOT leak into owner's list
    dj, fp = _both_get(fast, django, owner, "/zones")
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    assert dj.content == fp.content
    body = fp.json()
    assert {z["name"] for z in body} == {"Alpha", "Beta"}


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
