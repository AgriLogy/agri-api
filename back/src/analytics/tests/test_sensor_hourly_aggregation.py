"""Verify the per-sensor list endpoints aggregate to one averaged value per
hour by default, expose a ``raw=true`` escape hatch, and handle the NPK
multi-value sensor.

Covers ``apps.sensors.router_sensors._hourly_readings`` end-to-end through the
django-ninja route (JwtAuth via a real simplejwt access token).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from analytics.models import NpkSensor, TemperatureWeather


def _auth(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _token(user) -> str:
    return str(RefreshToken.for_user(user).access_token)


@pytest.mark.django_db
def test_hourly_average_one_row_per_hour(normal_user, zone_factory):
    zone = zone_factory(normal_user)
    # Hour 10:00 → values 10, 20 (avg 15); hour 11:00 → value 30 (avg 30).
    for ts, val in [
        (datetime(2026, 6, 9, 10, 5, tzinfo=timezone.utc), 10.0),
        (datetime(2026, 6, 9, 10, 55, tzinfo=timezone.utc), 20.0),
        (datetime(2026, 6, 9, 11, 30, tzinfo=timezone.utc), 30.0),
    ]:
        TemperatureWeather.objects.create(
            user=normal_user, zone=zone, value=val, timestamp=ts
        )

    client = APIClient()
    resp = client.get(
        "/sensors/temperatureweather", **_auth(_token(normal_user))
    )
    assert resp.status_code == 200, resp.content
    rows = resp.json()

    assert len(rows) == 2, rows
    assert rows[0]["timestamp"] == "2026-06-09T10:00:00Z"
    assert rows[0]["value"] == 15.0
    assert rows[1]["timestamp"] == "2026-06-09T11:00:00Z"
    assert rows[1]["value"] == 30.0
    # Shape parity with the raw serializer.
    assert "default_unit" in rows[0] and rows[0]["default_unit"] == "°C"
    assert rows[0]["zone"] == zone.id


@pytest.mark.django_db
def test_raw_escape_hatch_returns_every_reading(normal_user, zone_factory):
    zone = zone_factory(normal_user)
    for minute, val in [(5, 10.0), (55, 20.0)]:
        TemperatureWeather.objects.create(
            user=normal_user,
            zone=zone,
            value=val,
            timestamp=datetime(2026, 6, 9, 10, minute, tzinfo=timezone.utc),
        )

    client = APIClient()
    resp = client.get(
        "/sensors/temperatureweather?raw=true", **_auth(_token(normal_user))
    )
    assert resp.status_code == 200, resp.content
    rows = resp.json()
    assert len(rows) == 2  # both raw rows, not collapsed
    assert {r["value"] for r in rows} == {10.0, 20.0}


@pytest.mark.django_db
def test_npk_averages_each_component(normal_user, zone_factory):
    zone = zone_factory(normal_user)
    for n, p, k in [(10.0, 100.0, 1.0), (20.0, 200.0, 3.0)]:
        NpkSensor.objects.create(
            user=normal_user,
            zone=zone,
            nitrogen_value=n,
            phosphorus_value=p,
            potassium_value=k,
            timestamp=datetime(2026, 6, 9, 10, 5, tzinfo=timezone.utc),
        )

    client = APIClient()
    resp = client.get("/sensors/npk", **_auth(_token(normal_user)))
    assert resp.status_code == 200, resp.content
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["nitrogen_value"] == 15.0
    assert rows[0]["phosphorus_value"] == 150.0
    assert rows[0]["potassium_value"] == 2.0
    # NPK has no single ``value`` column.
    assert "value" not in rows[0]


@pytest.mark.django_db
def test_empty_returns_list(normal_user):
    client = APIClient()
    resp = client.get(
        "/sensors/temperatureweather", **_auth(_token(normal_user))
    )
    assert resp.status_code == 200
    assert resp.json() == []
