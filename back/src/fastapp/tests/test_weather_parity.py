"""F2 golden parity: /weather/et-forecast — fastapp must return byte-identical
responses to the Django ninja endpoint it replaces.

Each test drives BOTH surfaces over the SAME committed test data + the SAME
Django-minted access token: the Django route via DRF's APIClient (ninja is
mounted at the URL root), the fastapp route via Starlette's TestClient. Both
read the same Postgres (AGRI_DB_URL bound to Django's test DB by
back/conftest.py), so any drift in shape, ordering, rounding, or the 404
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

URL = "/weather/et-forecast"


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture(autouse=True)
def _disable_openmeteo(monkeypatch):
    # The Open-Meteo reference curve is a fastapp-only superset field fetched
    # over the network. Disable it so parity tests stay deterministic + offline;
    # ``et0_openmeteo_mm`` is then null on every day (asserted separately).
    monkeypatch.setenv("ET0_OPENMETEO", "off")


# fastapp evolved past byte-parity with the (now-legacy, unrouted) Django
# endpoint by adding a real Open-Meteo reference curve. These keys are the
# fastapp-only superset; strip them to compare the shared F2 contract.
_FASTAPP_ONLY_TOP = {"reference_provider"}
_FASTAPP_ONLY_DAY = {"et0_openmeteo_mm"}


def _shared_contract(body):
    """Drop fastapp-only superset fields so the core payload can be compared to
    the Django baseline."""
    if not isinstance(body, dict) or "days" not in body:
        return body
    trimmed = {k: v for k, v in body.items() if k not in _FASTAPP_ONLY_TOP}
    trimmed["days"] = [
        {k: v for k, v in day.items() if k not in _FASTAPP_ONLY_DAY}
        for day in trimmed["days"]
    ]
    return trimmed


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def django() -> APIClient:
    return APIClient()


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="wx-owner",
        email="wx-owner@example.com",
        password="irrelevant-3921",
        latitude=32.9,
        longitude=-6.9,
    )


@pytest.fixture
def other(django_user_model):
    return django_user_model.objects.create_user(
        username="wx-other",
        email="wx-other@example.com",
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
    return Zone.objects.create(user=user, **payload)


@pytest.fixture
def zone(owner):
    return _make_zone(owner)


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both(fast, django, user, path):
    """Return (django_response, fastapp_response) for the same path+token."""
    tok = _token(user)
    dj = django.get(path, HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.get(path, headers={"Authorization": f"Bearer {tok}"})
    return dj, fp


def test_forecast_body_is_identical(fast, django, owner, zone):
    dj, fp = _both(fast, django, owner, f"{URL}?zone_id={zone.id}")
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    # Shared F2 contract still matches the Django baseline byte-for-byte at the
    # value level, once fastapp's Open-Meteo superset fields are stripped.
    assert dj.json() == _shared_contract(fp.json())
    # sanity: the shared payload is actually the real thing, not two empties
    body = fp.json()
    assert body["zone_id"] == zone.id
    assert body["provider"] == "mock"
    assert len(body["days"]) == 7
    # fastapp superset: the reference curve fields are present (null here since
    # the network call is disabled in tests).
    assert body["reference_provider"] == "open-meteo"
    assert all("et0_openmeteo_mm" in day for day in body["days"])
    assert all(day["et0_openmeteo_mm"] is None for day in body["days"])


def test_days_clamp_is_identical(fast, django, owner, zone):
    for days in (3, 99, 0):
        dj, fp = _both(fast, django, owner, f"{URL}?zone_id={zone.id}&days={days}")
        assert dj.status_code == fp.status_code
        assert dj.json() == _shared_contract(fp.json())


def test_missing_zone_404_is_identical(fast, django, owner):
    dj, fp = _both(fast, django, owner, f"{URL}?zone_id=99999999")
    assert dj.status_code == 404
    assert fp.status_code == 404
    assert dj.json() == fp.json() == {"detail": "Zone not found."}
    assert dj.content == fp.content  # byte-identical 404 envelope


def test_other_users_zone_404_is_identical(fast, django, owner, other, zone):
    # `owner`'s token, `zone` belongs to owner — give `other` a zone and hit it
    foreign = _make_zone(other, name="Autre")
    dj, fp = _both(fast, django, owner, f"{URL}?zone_id={foreign.id}")
    assert dj.status_code == 404
    assert fp.status_code == 404
    assert dj.json() == fp.json() == {"detail": "Zone not found."}


def test_missing_auth_is_401_on_both(fast, django, zone):
    dj = django.get(f"{URL}?zone_id={zone.id}")
    fp = fast.get(f"{URL}?zone_id={zone.id}")
    assert dj.status_code == 401
    assert fp.status_code == 401
