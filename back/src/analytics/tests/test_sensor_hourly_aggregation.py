"""Verify the per-sensor list endpoints aggregate to one averaged value per
hour by default, expose a ``raw=true`` escape hatch, and handle the NPK
multi-value sensor.

The default (aggregated) path now delegates to agri-core
(``AgriMainDBClient.hourly_averages`` over the agri.db SQLAlchemy models) via
``apps.sensors.engine``. Those tests therefore need committed data visible to
agri-core's *separate* SQLAlchemy connection — so they use
``TransactionTestCase`` and skip on sqlite (they run on Postgres in CI, where
``conftest`` binds ``AGRI_DB_URL`` to Django's test DB). The ``raw=true`` and
empty paths stay on the Django ORM and run anywhere.
"""

from __future__ import annotations

import datetime as dt
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from analytics.models import NpkSensor, TemperatureWeather, Zone

_REQUIRES_PG = skipUnless(
    settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    "hourly aggregation delegates to agri-core's SQLAlchemy layer (Postgres only); runs in CI",
)

UTC = dt.timezone.utc
User = get_user_model()


def _bearer(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return client


def _user(username="agg-user"):
    return User.objects.create(
        username=username, email=f"{username}@example.com", is_active=True
    )


def _zone(user):
    return Zone.objects.create(
        user=user,
        name="agg-zone",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


@_REQUIRES_PG
class HourlyAggregationViaCoreTests(TransactionTestCase):
    """Default path: one averaged value per hour, computed by agri-core."""

    def setUp(self):
        self.user = _user()
        self.zone = _zone(self.user)
        self.client = _bearer(self.user)

    def test_one_row_per_hour(self):
        # 10:00 → avg(10, 20) = 15 ; 11:00 → avg(30) = 30
        for ts, v in [
            (dt.datetime(2026, 6, 9, 10, 5, tzinfo=UTC), 10.0),
            (dt.datetime(2026, 6, 9, 10, 55, tzinfo=UTC), 20.0),
            (dt.datetime(2026, 6, 9, 11, 30, tzinfo=UTC), 30.0),
        ]:
            TemperatureWeather.objects.create(
                user=self.user, zone=self.zone, value=v, timestamp=ts
            )

        resp = self.client.get("/sensors/temperatureweather")
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = resp.json()
        self.assertEqual([r["value"] for r in rows], [15.0, 30.0])
        self.assertEqual(rows[0]["default_unit"], "°C")
        self.assertEqual(rows[0]["zone"], self.zone.id)

    def test_npk_averages_each_component(self):
        for n, p, k in [(10.0, 100.0, 1.0), (20.0, 200.0, 3.0)]:
            NpkSensor.objects.create(
                user=self.user,
                zone=self.zone,
                nitrogen_value=n,
                phosphorus_value=p,
                potassium_value=k,
                timestamp=dt.datetime(2026, 6, 9, 10, 5, tzinfo=UTC),
            )

        resp = self.client.get("/sensors/npk")
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = resp.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nitrogen_value"], 15.0)
        self.assertEqual(rows[0]["phosphorus_value"], 150.0)
        self.assertEqual(rows[0]["potassium_value"], 2.0)
        self.assertNotIn("value", rows[0])

    def test_zone_filter(self):
        other = _zone(self.user)
        TemperatureWeather.objects.create(
            user=self.user,
            zone=self.zone,
            value=10.0,
            timestamp=dt.datetime(2026, 6, 9, 10, 5, tzinfo=UTC),
        )
        TemperatureWeather.objects.create(
            user=self.user,
            zone=other,
            value=99.0,
            timestamp=dt.datetime(2026, 6, 9, 10, 6, tzinfo=UTC),
        )
        resp = self.client.get(f"/sensors/temperatureweather?zone={self.zone.id}")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([r["value"] for r in resp.json()], [10.0])


class RawAndEmptyTests(TestCase):
    """Django-ORM paths — no agri-core delegation, so they run on sqlite too."""

    def setUp(self):
        self.user = _user("raw-user")
        self.zone = _zone(self.user)
        self.client = _bearer(self.user)

    def test_raw_returns_every_reading(self):
        for minute, v in [(5, 10.0), (55, 20.0)]:
            TemperatureWeather.objects.create(
                user=self.user,
                zone=self.zone,
                value=v,
                timestamp=dt.datetime(2026, 6, 9, 10, minute, tzinfo=UTC),
            )
        resp = self.client.get("/sensors/temperatureweather?raw=true")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual(len(rows), 2)  # both raw rows, not collapsed
        self.assertEqual({r["value"] for r in rows}, {10.0, 20.0})

    def test_empty_returns_list(self):
        # Default path with no data short-circuits in the adapter (no core call).
        resp = self.client.get("/sensors/temperatureweather")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])
