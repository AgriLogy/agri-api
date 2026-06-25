"""Tests for the single-sensor ingest webhook (POST /ingest/sensor).

Covers the water-level sensor path (epic #4 / the #37 "extend push beyond
the weather metrics" item): a reading POSTed by a device is persisted and
pushed through the alert dispatcher so threshold alerts fire.
"""

from __future__ import annotations

from decimal import Decimal

from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from analytics.models import Alert, Zone
from apps.sensors.models import WaterLevelSensor
from apps.users.models import CustomUser


def _user(username="tanker"):
    u = CustomUser.objects.create(
        username=username,
        email=f"{username}@example.com",
        firstname=username.title(),
        lastname="Doe",
        is_active=True,
    )
    u.set_password("pw")
    u.save()
    return u


def _zone(user):
    return Zone.objects.create(
        user=user,
        name="basin",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _low_level_alert(user, zone):
    return Alert.objects.create(
        name="Low basin",
        type="Flow",
        description="",
        condition="<",
        condition_nbr=Decimal("0.50"),
        sensor_key="water_level",
        zone=zone,
        user=user,
        is_active=True,
    )


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SensorIngestTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.zone = _zone(self.user)
        self.api = APIClient()

    def _post(self, **body):
        return self.api.post("/ingest/sensor", body, format="json")

    def test_water_level_reading_is_stored(self):
        r = self._post(client="tanker", sensor_key="water_level", value=1.42)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["inserted"], 1)
        row = WaterLevelSensor.objects.get()
        self.assertEqual(row.value, 1.42)
        self.assertEqual(row.zone_id, self.zone.id)
        self.assertEqual(row.user_id, self.user.id)

    def test_low_level_fires_alert(self):
        alert = _low_level_alert(self.user, self.zone)
        mail.outbox.clear()
        r = self._post(client="tanker", sensor_key="water_level", value=0.2)
        self.assertEqual(r.status_code, 201)
        # Deterministic: dispatch's conditional UPDATE stamps last_emailed_at
        # the moment the alert fires, independent of the Celery broker.
        alert.refresh_from_db()
        self.assertIsNotNone(alert.last_emailed_at)
        # And with eager Celery + locmem the email actually lands.
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Low basin", mail.outbox[0].subject)

    def test_above_threshold_does_not_fire(self):
        alert = _low_level_alert(self.user, self.zone)
        mail.outbox.clear()
        r = self._post(client="tanker", sensor_key="water_level", value=2.0)
        self.assertEqual(r.status_code, 201)
        alert.refresh_from_db()
        self.assertIsNone(alert.last_emailed_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_unknown_sensor_key_rejected(self):
        r = self._post(client="tanker", sensor_key="nonsense", value=1.0)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(WaterLevelSensor.objects.count(), 0)

    def test_npk_rejected(self):
        r = self._post(client="tanker", sensor_key="npk", value=1.0)
        self.assertEqual(r.status_code, 400)

    def test_unknown_client_rejected(self):
        r = self._post(client="ghost", sensor_key="water_level", value=1.0)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(WaterLevelSensor.objects.count(), 0)

    def test_missing_value_rejected(self):
        r = self._post(client="tanker", sensor_key="water_level")
        self.assertEqual(r.status_code, 422)
