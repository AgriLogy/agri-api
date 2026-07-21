"""Tests for the fastapp /sectors router (user-owned zone grouping).

Dual-ORM: needs Postgres (fastapp reads via SQLAlchemy the rows Django writes)
+ committed data (``django_db(transaction=True)``).
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
    reason="dual-ORM: fastapp reads the test DB Django writes (needs Postgres)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="sec-owner",
        email="sec-owner@example.com",
        password="irrelevant-3921",
        firstname="Sec",
        lastname="Owner",
        phone_number="+212600000021",
    )


@pytest.fixture
def other(django_user_model):
    return django_user_model.objects.create_user(
        username="sec-other",
        email="sec-other@example.com",
        password="irrelevant-3921",
    )


def _make_zone(user, name):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=user,
        name=name,
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
        elevation_m=120.0,
    )


def _auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


def test_sector_crud_and_zone_assignment(fast, owner):
    h = _auth(owner)
    z1 = _make_zone(owner, "z1")
    z2 = _make_zone(owner, "z2")

    # create
    r = fast.post("/sectors", json={"name": "North"}, headers=h)
    assert r.status_code == 201, r.text
    sec = r.json()
    assert sec["name"] == "North" and sec["zone_count"] == 0
    sid = sec["id"]

    # list
    r = fast.get("/sectors", headers=h)
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == [sid]

    # rename
    r = fast.patch(f"/sectors/{sid}", json={"name": "North Field"}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "North Field"

    # assign both zones
    r = fast.put(f"/sectors/{sid}/zones", json={"zone_ids": [z1.id, z2.id]}, headers=h)
    assert r.status_code == 200 and r.json()["zone_count"] == 2

    # /zones now reports the sector
    zones = {z["id"]: z for z in fast.get("/zones", headers=h).json()}
    assert zones[z1.id]["sector_id"] == sid
    assert zones[z1.id]["sector_name"] == "North Field"

    # reassign to just z1 → z2 becomes unassigned
    r = fast.put(f"/sectors/{sid}/zones", json={"zone_ids": [z1.id]}, headers=h)
    assert r.json()["zone_count"] == 1
    zones = {z["id"]: z for z in fast.get("/zones", headers=h).json()}
    assert zones[z2.id]["sector_id"] is None

    # delete → remaining zone unassigned
    r = fast.delete(f"/sectors/{sid}", headers=h)
    assert r.status_code == 200
    zones = {z["id"]: z for z in fast.get("/zones", headers=h).json()}
    assert zones[z1.id]["sector_id"] is None
    assert fast.get("/sectors", headers=h).json() == []


def test_sector_is_owner_scoped(fast, owner, other):
    sid = fast.post("/sectors", json={"name": "Mine"}, headers=_auth(owner)).json()[
        "id"
    ]
    # other user cannot see it
    assert fast.get("/sectors", headers=_auth(other)).json() == []
    # nor rename/delete it (404, not 403 — it simply isn't theirs)
    assert (
        fast.patch(
            f"/sectors/{sid}", json={"name": "Hijack"}, headers=_auth(other)
        ).status_code
        == 404
    )
    assert fast.delete(f"/sectors/{sid}", headers=_auth(other)).status_code == 404


def test_farm_overview_structure(fast, owner):
    h = _auth(owner)
    z_in = _make_zone(owner, "in-sector")
    _make_zone(owner, "loose")  # stays unassigned
    sid = fast.post("/sectors", json={"name": "North"}, headers=h).json()["id"]
    fast.put(f"/sectors/{sid}/zones", json={"zone_ids": [z_in.id]}, headers=h)

    r = fast.get("/sectors/overview", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    # one sector node + one "unassigned" (sector_id None) bucket
    named = {n["sector_name"]: n for n in body}
    assert "North" in named
    assert named["North"]["zones"][0]["zone_name"] == "in-sector"
    unassigned = [n for n in body if n["sector_id"] is None]
    assert unassigned and unassigned[0]["zones"][0]["zone_name"] == "loose"
    # captors present as a list (empty here — no readings/config in the test)
    assert named["North"]["zones"][0]["captors"] == []


def test_assigning_foreign_zone_is_ignored(fast, owner, other):
    sid = fast.post("/sectors", json={"name": "S"}, headers=_auth(owner)).json()["id"]
    foreign = _make_zone(other, "foreign")
    # owner tries to pull another user's zone into their sector → silently ignored
    r = fast.put(
        f"/sectors/{sid}/zones", json={"zone_ids": [foreign.id]}, headers=_auth(owner)
    )
    assert r.status_code == 200 and r.json()["zone_count"] == 0
