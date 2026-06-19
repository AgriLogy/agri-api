"""Behaviour tests for the crop-calendar (Kc) CRUD endpoints (/kc)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from analytics.models import Kc, KcPeriod, Zone

User = get_user_model()


def _authed(user) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return c


@pytest.fixture
def user(db):
    u = User.objects.create(username="kc_owner", email="kc@e.com", is_active=True)
    u.set_password("pw")
    u.save()
    return u


@pytest.fixture
def zone(user):
    return Zone.objects.create(
        user=user, name="North field", space=1000.0, critical_moisture_threshold=25.0
    )


_PERIODS = [
    {
        "period_name": "Initial",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "kc_value": 0.4,
    },
    {
        "period_name": "Mid-season",
        "start_date": "2026-04-01",
        "end_date": "2026-05-31",
        "kc_value": 1.1,
    },
]


@pytest.mark.django_db
class TestKcCrud:
    def test_create_with_periods(self, user, zone):
        c = _authed(user)
        r = c.post(
            "/kc",
            {
                "name": "Tomato 2026",
                "plant_name": "Tomato",
                "zone_id": zone.id,
                "periods": _PERIODS,
            },
            format="json",
        )
        assert r.status_code == 201, r.content
        body = r.json()
        assert body["name"] == "Tomato 2026"
        assert body["zone_id"] == zone.id and body["zone_name"] == "North field"
        assert body["number_of_periods"] == 2
        assert [p["period_name"] for p in body["periods"]] == ["Initial", "Mid-season"]
        assert KcPeriod.objects.count() == 2

    def test_list_is_caller_scoped(self, user, zone):
        other = User.objects.create(username="kc_other", email="o@e.com")
        Kc.objects.create(user=other, name="Not mine", plant_name="X", zone=None)
        c = _authed(user)
        c.post(
            "/kc",
            {"name": "Mine", "plant_name": "Y", "zone_id": zone.id, "periods": []},
            format="json",
        )
        rows = c.get("/kc").json()
        assert [k["name"] for k in rows] == ["Mine"]

    def test_zone_filter(self, user, zone):
        z2 = Zone.objects.create(
            user=user, name="South", space=500.0, critical_moisture_threshold=25.0
        )
        c = _authed(user)
        c.post(
            "/kc",
            {"name": "A", "plant_name": "P", "zone_id": zone.id, "periods": []},
            format="json",
        )
        c.post(
            "/kc",
            {"name": "B", "plant_name": "P", "zone_id": z2.id, "periods": []},
            format="json",
        )
        rows = c.get(f"/kc?zone_id={z2.id}").json()
        assert [k["name"] for k in rows] == ["B"]

    def test_update_replaces_periods(self, user, zone):
        c = _authed(user)
        kc_id = c.post(
            "/kc",
            {
                "name": "Crop",
                "plant_name": "P",
                "zone_id": zone.id,
                "periods": _PERIODS,
            },
            format="json",
        ).json()["id"]
        r = c.put(
            f"/kc/{kc_id}",
            {
                "name": "Crop v2",
                "plant_name": "P",
                "zone_id": zone.id,
                "periods": [_PERIODS[0]],
            },
            format="json",
        )
        assert r.status_code == 200
        assert r.json()["number_of_periods"] == 1
        assert KcPeriod.objects.count() == 1  # old periods cleaned up

    def test_delete_cascades_periods(self, user, zone):
        c = _authed(user)
        kc_id = c.post(
            "/kc",
            {
                "name": "Crop",
                "plant_name": "P",
                "zone_id": zone.id,
                "periods": _PERIODS,
            },
            format="json",
        ).json()["id"]
        assert c.delete(f"/kc/{kc_id}").status_code == 200
        assert Kc.objects.count() == 0 and KcPeriod.objects.count() == 0

    def test_cannot_touch_others_kc(self, user):
        other = User.objects.create(username="kc_other2", email="o2@e.com")
        kc = Kc.objects.create(user=other, name="Theirs", plant_name="X", zone=None)
        c = _authed(user)
        assert c.get(f"/kc/{kc.id}").status_code == 404
        assert c.delete(f"/kc/{kc.id}").status_code == 404
