"""PATCH /zones/{zone_id}: a farmer renames their own zone (agri-web#172).

Dual-ORM like test_sectors: Django writes the users/zones, fastapp reads them
through SQLAlchemy, so Postgres + committed data.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from django.db import connection
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM: fastapp reads the test DB Django writes (needs Postgres)",
)
pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


def _user(django_user_model, name, **extra):
    return django_user_model.objects.create_user(
        username=name, email=f"{name}@example.com", password="irrelevant-3921", **extra
    )


def _zone(user, name="Parcelle 1"):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=user, name=name, space=1000.0, critical_moisture_threshold=20.0
    )


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


def _name(zone_id) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM analytics_zone WHERE id = %s", [zone_id])
        return cursor.fetchone()[0]


def test_owner_renames_and_it_is_audited(fast, django_user_model):
    owner = _user(django_user_model, "zone-owner")  # default level: editor
    zone = _zone(owner)
    r = fast.patch(
        f"/zones/{zone.id}", json={"name": "  Olivier nord  "}, headers=_auth(owner)
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"id": zone.id, "name": "Olivier nord", "sector_id": None}
    assert _name(zone.id) == "Olivier nord"
    # The listing farmers read reflects it.
    listed = fast.get("/zones", headers=_auth(owner)).json()
    assert [z["name"] for z in listed] == ["Olivier nord"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT action, target_id FROM analytics_auditevent "
            "WHERE actor_id = %s ORDER BY id DESC LIMIT 1",
            [owner.id],
        )
        assert cursor.fetchone() == ("zone.rename", str(zone.id))


def test_someone_elses_zone_is_not_found(fast, django_user_model):
    owner = _user(django_user_model, "zone-owner")
    other = _user(django_user_model, "zone-other")
    zone = _zone(owner)
    r = fast.patch(f"/zones/{zone.id}", json={"name": "x"}, headers=_auth(other))
    assert r.status_code == 404
    assert _name(zone.id) == "Parcelle 1"


@pytest.mark.parametrize(
    "body", [{"name": ""}, {"name": "   "}, {"name": "x" * 101}, {}]
)
def test_invalid_names_are_rejected(fast, django_user_model, body):
    owner = _user(django_user_model, "zone-owner")
    zone = _zone(owner)
    r = fast.patch(f"/zones/{zone.id}", json=body, headers=_auth(owner))
    assert r.status_code in (400, 422), r.text
    assert _name(zone.id) == "Parcelle 1"


def test_monitor_level_and_technician_cannot_rename(
    fast, django_user_model, set_access_level
):
    monitor = _user(django_user_model, "zone-monitor")
    set_access_level(monitor, "monitor")
    zone = _zone(monitor)
    assert (
        fast.patch(f"/zones/{zone.id}", json={"name": "x"}, headers=_auth(monitor))
    ).status_code == 403

    tech = _user(django_user_model, "zone-tech", is_technician=True)
    tech_zone = _zone(tech)
    assert (
        fast.patch(f"/zones/{tech_zone.id}", json={"name": "x"}, headers=_auth(tech))
    ).status_code == 403
    assert _name(zone.id) == _name(tech_zone.id) == "Parcelle 1"


def test_requires_authentication(fast, django_user_model):
    zone = _zone(_user(django_user_model, "zone-owner"))
    assert fast.patch(f"/zones/{zone.id}", json={"name": "x"}).status_code == 401
