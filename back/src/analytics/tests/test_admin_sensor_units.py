"""Tests for /users/<username>/sensor-units (django-ninja, JWT bearer auth)."""

import pytest

from analytics.models import UserSensorUnitPreference


def _url(username: str) -> str:
    return f"/users/{username}/sensor-units"


@pytest.mark.django_db
class TestAdminSensorUnits:
    def test_anonymous_is_401(self, anon_client, normal_user):
        resp = anon_client.get(_url(normal_user.username))
        assert resp.status_code == 401

    def test_user_is_403(self, user_bearer, other_user):
        resp = user_bearer.get(_url(other_user.username))
        assert resp.status_code == 403

    def test_admin_get_returns_dict(self, admin_bearer, normal_user):
        resp = admin_bearer.get(_url(normal_user.username))
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_admin_patch_persists(self, admin_bearer, normal_user):
        resp = admin_bearer.patch(
            _url(normal_user.username),
            {"temperature_weather": "°F", "wind_speed": "km/h"},
            format="json",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["temperature_weather"] == "°F"
        assert body["wind_speed"] == "km/h"
        assert UserSensorUnitPreference.objects.filter(user=normal_user).count() == 2

    def test_patch_idempotent(self, admin_bearer, normal_user):
        admin_bearer.patch(
            _url(normal_user.username), {"wind_speed": "km/h"}, format="json"
        )
        resp = admin_bearer.patch(
            _url(normal_user.username), {"wind_speed": "m/s"}, format="json"
        )
        assert resp.status_code == 200
        assert resp.json()["wind_speed"] == "m/s"
        assert UserSensorUnitPreference.objects.filter(user=normal_user).count() == 1

    def test_patch_rejects_non_object(self, admin_bearer, normal_user):
        resp = admin_bearer.patch(_url(normal_user.username), ["bad"], format="json")
        assert resp.status_code == 400
