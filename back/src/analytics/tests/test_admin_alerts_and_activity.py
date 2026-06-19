"""Tests for admin alert override + per-user activity timeline (django-ninja, JWT bearer auth).

New ninja routes (analytics.router_admin, mounted at root):
  * GET                 /users/<username>/alerts   (admin: a user's alerts)
  * GET, PATCH, DELETE  /admin/alerts/<pk>         (admin: alert override)
  * GET                 /users/<username>/activity (admin: activity timeline)
All require JWT + is_staff (403 for non-staff, 401 for anonymous).
"""

import pytest


def _user_alerts_url(username):
    return f"/users/{username}/alerts"


def _alert_detail_url(pk):
    return f"/admin/alerts/{pk}"


def _user_activity_url(username):
    return f"/users/{username}/activity"


@pytest.mark.django_db
class TestAdminUserAlerts:
    def test_user_is_403(self, user_bearer, other_user):
        resp = user_bearer.get(_user_alerts_url(other_user.username))
        assert resp.status_code == 403

    def test_admin_lists_only_target_user_alerts(
        self, admin_bearer, normal_user, other_user, alert_factory
    ):
        alert_factory(normal_user, name="A")
        alert_factory(other_user, name="B")
        resp = admin_bearer.get(_user_alerts_url(normal_user.username))
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        names = {r["name"] for r in rows}
        assert "A" in names
        assert "B" not in names


@pytest.mark.django_db
class TestAdminAlertCreate:
    URL = "/admin/alerts"

    def test_user_is_403(self, user_bearer, normal_user):
        resp = user_bearer.post(
            self.URL,
            {
                "username": normal_user.username,
                "name": "X",
                "type": "Humidity",
                "condition": ">",
                "condition_nbr": 50,
            },
            format="json",
        )
        assert resp.status_code == 403

    def test_admin_creates_alert(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        resp = admin_bearer.post(
            self.URL,
            {
                "username": normal_user.username,
                "name": "Heat",
                "type": "Weather Temperature",
                "condition": ">",
                "condition_nbr": 30,
                "sensor_key": "temperature_weather",
                "zone_id": zone.id,
            },
            format="json",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Heat"
        assert body["username"] == normal_user.username
        assert body["zone"] == zone.id

    def test_unknown_user_is_404(self, admin_bearer):
        resp = admin_bearer.post(
            self.URL,
            {
                "username": "ghost",
                "name": "X",
                "type": "Humidity",
                "condition": ">",
                "condition_nbr": 50,
            },
            format="json",
        )
        assert resp.status_code == 404

    def test_foreign_zone_is_404(
        self, admin_bearer, normal_user, other_user, zone_factory
    ):
        foreign = zone_factory(other_user)
        resp = admin_bearer.post(
            self.URL,
            {
                "username": normal_user.username,
                "name": "X",
                "type": "Humidity",
                "condition": ">",
                "condition_nbr": 50,
                "zone_id": foreign.id,
            },
            format="json",
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestAdminAlertDetail:
    def test_admin_can_toggle_active(self, admin_bearer, normal_user, alert_factory):
        alert = alert_factory(normal_user)
        resp = admin_bearer.patch(
            _alert_detail_url(alert.pk), {"is_active": False}, format="json"
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        alert.refresh_from_db()
        assert alert.is_active is False

    def test_admin_can_delete(self, admin_bearer, normal_user, alert_factory):
        alert = alert_factory(normal_user)
        resp = admin_bearer.delete(_alert_detail_url(alert.pk))
        assert resp.status_code == 204

    def test_user_is_403(self, user_bearer, normal_user, alert_factory):
        alert = alert_factory(normal_user)
        resp = user_bearer.patch(
            _alert_detail_url(alert.pk), {"is_active": False}, format="json"
        )
        assert resp.status_code == 403


@pytest.mark.django_db
class TestAdminUserActivity:
    def test_anonymous_is_401(self, anon_client, normal_user):
        resp = anon_client.get(_user_activity_url(normal_user.username))
        assert resp.status_code == 401

    def test_admin_gets_events(self, admin_bearer, normal_user, zone_factory):
        zone_factory(normal_user)
        resp = admin_bearer.get(_user_activity_url(normal_user.username))
        assert resp.status_code == 200
        body = resp.json()
        assert "events" in body and isinstance(body["events"], list)
        kinds = {e["kind"] for e in body["events"]}
        assert "joined" in kinds
        assert "zones" in kinds
