"""Tests for the admin sensor-data explorer (django-ninja)."""

from datetime import datetime, timezone

import pytest

from apps.sensors.models import TemperatureWeather

BASE = "/admin/sensor-data"
SENSOR = "temperatureweather"  # _slug_for(TemperatureWeather)


def _reading(user, zone, value, when):
    return TemperatureWeather.objects.create(
        user=user, zone=zone, value=value, timestamp=when
    )


@pytest.mark.django_db
class TestAdminSensorData:
    def test_normal_user_is_403(self, user_bearer):
        assert user_bearer.get(f"{BASE}/catalog").status_code == 403
        assert user_bearer.get(f"{BASE}?sensor={SENSOR}").status_code == 403

    def test_catalog_lists_sensors(self, admin_bearer):
        res = admin_bearer.get(f"{BASE}/catalog")
        assert res.status_code == 200
        slugs = {s["slug"] for s in res.json()["sensors"]}
        assert SENSOR in slugs
        assert "battery" in slugs and "signal" in slugs

    def test_list_filters_by_user_and_zone(
        self, admin_bearer, normal_user, other_user, zone_factory
    ):
        z1 = zone_factory(normal_user)
        z2 = zone_factory(other_user)
        _reading(normal_user, z1, 21.0, datetime(2026, 6, 1, tzinfo=timezone.utc))
        _reading(other_user, z2, 99.0, datetime(2026, 6, 1, tzinfo=timezone.utc))

        rows = admin_bearer.get(
            f"{BASE}?sensor={SENSOR}&username={normal_user.username}"
        ).json()["rows"]
        assert len(rows) == 1 and rows[0]["value"] == 21.0

        rows = admin_bearer.get(f"{BASE}?sensor={SENSOR}&zone_id={z2.id}").json()[
            "rows"
        ]
        assert len(rows) == 1 and rows[0]["value"] == 99.0

    def test_unknown_sensor_404(self, admin_bearer):
        assert admin_bearer.get(f"{BASE}?sensor=nope").status_code == 404

    def test_patch_value(self, admin_bearer, normal_user, zone_factory):
        z = zone_factory(normal_user)
        r = _reading(normal_user, z, 10.0, datetime(2026, 6, 2, tzinfo=timezone.utc))
        res = admin_bearer.patch(
            f"{BASE}/{SENSOR}/{r.id}", {"value": 12.5}, format="json"
        )
        assert res.status_code == 200 and res.json()["value"] == 12.5
        r.refresh_from_db()
        assert r.value == 12.5

    def test_delete_one(self, admin_bearer, normal_user, zone_factory):
        z = zone_factory(normal_user)
        r = _reading(normal_user, z, 10.0, datetime(2026, 6, 2, tzinfo=timezone.utc))
        assert admin_bearer.delete(f"{BASE}/{SENSOR}/{r.id}").status_code == 200
        assert not TemperatureWeather.objects.filter(pk=r.id).exists()

    def test_range_delete_requires_guard(self, admin_bearer, normal_user, zone_factory):
        z = zone_factory(normal_user)
        # No zone_id / range → 400, nothing deleted.
        assert admin_bearer.delete(f"{BASE}/{SENSOR}").status_code == 400

    def test_range_delete(self, admin_bearer, normal_user, zone_factory):
        z = zone_factory(normal_user)
        _reading(normal_user, z, 1.0, datetime(2026, 6, 1, tzinfo=timezone.utc))
        _reading(normal_user, z, 2.0, datetime(2026, 6, 5, tzinfo=timezone.utc))
        _reading(normal_user, z, 3.0, datetime(2026, 6, 20, tzinfo=timezone.utc))
        res = admin_bearer.delete(
            f"{BASE}/{SENSOR}?zone_id={z.id}&from=2026-06-02&to=2026-06-10"
        )
        assert res.status_code == 200 and res.json()["deleted"] == 1
        remaining = sorted(
            TemperatureWeather.objects.filter(zone=z).values_list("value", flat=True)
        )
        assert remaining == [1.0, 3.0]
