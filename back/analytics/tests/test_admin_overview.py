"""Tests for GET /api/admin/overview/."""

import pytest
from django.urls import reverse


@pytest.mark.django_db
class TestAdminOverview:
    def test_anonymous_is_401(self, anon_client):
        url = reverse("admin-overview")
        resp = anon_client.get(url)
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_client):
        url = reverse("admin-overview")
        resp = user_client.get(url)
        assert resp.status_code == 403

    def test_admin_returns_kpis(
        self, admin_client, admin_user, normal_user, other_user, zone_factory
    ):
        zone_factory(normal_user)
        zone_factory(other_user)
        url = reverse("admin-overview")
        resp = admin_client.get(url)
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
