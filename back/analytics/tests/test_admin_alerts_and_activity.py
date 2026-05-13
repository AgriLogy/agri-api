"""Tests for admin alert override + per-user activity timeline."""

import pytest
from django.urls import reverse


@pytest.mark.django_db
class TestAdminUserAlerts:
    def test_user_is_403(self, user_client, other_user):
        url = reverse("admin-user-alerts", kwargs={"username": other_user.username})
        resp = user_client.get(url)
        assert resp.status_code == 403

    def test_admin_lists_only_target_user_alerts(
        self, admin_client, normal_user, other_user, alert_factory
    ):
        alert_factory(normal_user, name="A")
        alert_factory(other_user, name="B")
        url = reverse("admin-user-alerts", kwargs={"username": normal_user.username})
        resp = admin_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        names = {r["name"] for r in rows}
        assert "A" in names
        assert "B" not in names


@pytest.mark.django_db
class TestAdminAlertDetail:
    def test_admin_can_toggle_active(self, admin_client, normal_user, alert_factory):
        alert = alert_factory(normal_user)
        url = reverse("admin-alert-detail", kwargs={"pk": alert.pk})
        resp = admin_client.patch(url, {"is_active": False}, format="json")
        assert resp.status_code == 200
        alert.refresh_from_db()
        assert alert.is_active is False

    def test_admin_can_delete(self, admin_client, normal_user, alert_factory):
        alert = alert_factory(normal_user)
        url = reverse("admin-alert-detail", kwargs={"pk": alert.pk})
        resp = admin_client.delete(url)
        assert resp.status_code == 204

    def test_user_is_403(self, user_client, normal_user, alert_factory):
        alert = alert_factory(normal_user)
        url = reverse("admin-alert-detail", kwargs={"pk": alert.pk})
        resp = user_client.patch(url, {"is_active": False}, format="json")
        assert resp.status_code == 403


@pytest.mark.django_db
class TestAdminUserActivity:
    def test_anonymous_is_401(self, anon_client, normal_user):
        url = reverse("admin-user-activity", kwargs={"username": normal_user.username})
        resp = anon_client.get(url)
        assert resp.status_code == 401

    def test_admin_gets_events(self, admin_client, normal_user, zone_factory):
        zone_factory(normal_user)
        url = reverse("admin-user-activity", kwargs={"username": normal_user.username})
        resp = admin_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        assert "events" in body and isinstance(body["events"], list)
        kinds = {e["kind"] for e in body["events"]}
        assert "joined" in kinds
        assert "zones" in kinds
