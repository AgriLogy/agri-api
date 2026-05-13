"""Tests for /api/admin/users/<u>/sensor-units/."""

import pytest
from django.urls import reverse

from analytics.models import UserSensorUnitPreference


@pytest.mark.django_db
class TestAdminSensorUnits:
    def test_anonymous_is_401(self, anon_client, normal_user):
        url = reverse(
            "admin-user-sensor-units", kwargs={"username": normal_user.username}
        )
        resp = anon_client.get(url)
        assert resp.status_code == 401

    def test_user_is_403(self, user_client, other_user):
        url = reverse(
            "admin-user-sensor-units", kwargs={"username": other_user.username}
        )
        resp = user_client.get(url)
        assert resp.status_code == 403

    def test_admin_get_returns_dict(self, admin_client, normal_user):
        url = reverse(
            "admin-user-sensor-units", kwargs={"username": normal_user.username}
        )
        resp = admin_client.get(url)
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_admin_patch_persists(self, admin_client, normal_user):
        url = reverse(
            "admin-user-sensor-units", kwargs={"username": normal_user.username}
        )
        resp = admin_client.patch(
            url, {"temperature_weather": "°F", "wind_speed": "km/h"}, format="json"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["temperature_weather"] == "°F"
        assert body["wind_speed"] == "km/h"
        assert UserSensorUnitPreference.objects.filter(user=normal_user).count() == 2

    def test_patch_idempotent(self, admin_client, normal_user):
        url = reverse(
            "admin-user-sensor-units", kwargs={"username": normal_user.username}
        )
        admin_client.patch(url, {"wind_speed": "km/h"}, format="json")
        resp = admin_client.patch(url, {"wind_speed": "m/s"}, format="json")
        assert resp.status_code == 200
        assert resp.json()["wind_speed"] == "m/s"
        assert UserSensorUnitPreference.objects.filter(user=normal_user).count() == 1

    def test_patch_rejects_non_object(self, admin_client, normal_user):
        url = reverse(
            "admin-user-sensor-units", kwargs={"username": normal_user.username}
        )
        resp = admin_client.patch(url, ["bad"], format="json")
        assert resp.status_code == 400
