"""Tests for the admin analytics + global-alerts endpoints (django-ninja)."""

import pytest

ANALYTICS_URL = "/admin/analytics"
ALERTS_URL = "/admin/alerts"
ALERTS_ANALYTICS_URL = "/admin/alert-analytics"


@pytest.mark.django_db
class TestAdminAnalytics:
    def test_anonymous_is_401(self, anon_client):
        assert anon_client.get(ANALYTICS_URL).status_code == 401

    def test_normal_user_is_403(self, user_bearer):
        assert user_bearer.get(ANALYTICS_URL).status_code == 403

    def test_admin_returns_aggregates(
        self, admin_bearer, normal_user, other_user, zone_factory, alert_factory
    ):
        zone_factory(normal_user)
        zone_factory(other_user)
        alert_factory(normal_user, type="Weather Temperature")
        resp = admin_bearer.get(ANALYTICS_URL)
        assert resp.status_code == 200
        body = resp.json()
        for key in (
            "payment_status",
            "signups_by_week",
            "active_users",
            "inactive_users",
            "zones_per_user",
            "alerts_by_type",
            "devices",
        ):
            assert key in body
        # normal_user defaults to payement_status "actif".
        assert body["payment_status"].get("actif", 0) >= 1
        assert body["active_users"] >= 2
        assert set(body["zones_per_user"]) == {"0", "1", "2-3", "4+"}
        assert {"total", "stale", "online"} <= set(body["devices"])


@pytest.mark.django_db
class TestGlobalAlertsConsole:
    def test_normal_user_is_403(self, user_bearer):
        assert user_bearer.get(ALERTS_URL).status_code == 403

    def test_admin_lists_across_users(
        self, admin_bearer, normal_user, other_user, alert_factory
    ):
        alert_factory(normal_user, name="A")
        alert_factory(other_user, name="B")
        resp = admin_bearer.get(ALERTS_URL)
        assert resp.status_code == 200
        rows = resp.json()
        usernames = {r["username"] for r in rows}
        assert {normal_user.username, other_user.username} <= usernames

    def test_filters_by_username_and_active(
        self, admin_bearer, normal_user, other_user, alert_factory
    ):
        alert_factory(normal_user, name="mine", is_active=True)
        alert_factory(other_user, name="theirs")
        resp = admin_bearer.get(f"{ALERTS_URL}?username={normal_user.username}")
        rows = resp.json()
        assert rows and all(r["username"] == normal_user.username for r in rows)

        inactive = alert_factory(normal_user, name="off", is_active=False)
        rows = admin_bearer.get(f"{ALERTS_URL}?is_active=false").json()
        assert any(r["id"] == inactive.id for r in rows)
        assert all(r["is_active"] is False for r in rows)

    def test_analytics_distributions(self, admin_bearer, normal_user, alert_factory):
        from django.utils import timezone

        alert_factory(normal_user, type="Weather Temperature", sensor_key="t")
        alert_factory(normal_user, type="Pressure", sensor_key="p")
        alert_factory(
            normal_user,
            type="Pressure",
            sensor_key="p",
            last_triggered_at=timezone.now(),
        )
        body = admin_bearer.get(ALERTS_ANALYTICS_URL).json()
        assert body["total"] >= 3
        assert body["triggered_ever"] >= 1
        by_type = {row["type"]: row["count"] for row in body["by_type"]}
        assert by_type.get("Pressure", 0) >= 2
        assert any(r["sensor_key"] == "p" for r in body["by_sensor"])
        assert len(body["recently_triggered"]) >= 1
