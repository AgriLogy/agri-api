"""Tests for GET /admin/overview (django-ninja, JWT bearer auth)."""

import pytest

URL = "/admin/overview"


@pytest.mark.django_db
class TestAdminOverview:
    def test_anonymous_is_401(self, anon_client):
        resp = anon_client.get(URL)
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_bearer):
        resp = user_bearer.get(URL)
        assert resp.status_code == 403

    def test_admin_returns_kpis(
        self, admin_bearer, admin_user, normal_user, other_user, zone_factory
    ):
        zone_factory(normal_user)
        zone_factory(other_user)
        resp = admin_bearer.get(URL)
        assert resp.status_code == 200
        body = resp.json()
        for key in (
            "users_total",
            "users_active",
            "staff_total",
            "zones_total",
            "alerts_24h",
        ):
            assert key in body
            assert isinstance(body[key], int)
        assert body["users_total"] >= 3
        assert body["staff_total"] >= 1
        assert body["zones_total"] >= 2
