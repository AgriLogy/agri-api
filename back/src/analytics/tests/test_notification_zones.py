"""Custom notification zones (agrilogy-front #57): API + alert binding +
ingest dispatch + the SMS fan-out channel.

Run:
    SECRET_KEY=k DEBUG=True ALLOWED_HOSTS=* USE_POSTGRES=False \
        EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
        CELERY_TASK_ALWAYS_EAGER=True \
        uv run pytest src/analytics/tests/test_notification_zones.py
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.alerts.engine import dispatch_alerts_for_reading
from analytics.models import (
    Alert,
    NotificationZone,
    NotificationZoneSensor,
    TemperatureWeather,
    Zone,
)

User = get_user_model()


def _user(username="alice"):
    u = User.objects.create(
        username=username,
        email=f"{username}@example.com",
        firstname=username.title(),
        lastname="Doe",
        is_active=True,
    )
    u.set_password("pw")
    u.save()
    return u


def _zone(user, name="zone-A"):
    return Zone.objects.create(
        user=user,
        name=name,
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _authed_client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return c


class TestNotificationZoneAPI(TestCase):
    def setUp(self):
        self.user = _user("alice")
        self.zone = _zone(self.user)
        self.client = _authed_client(self.user)

    def test_create_with_sensors(self):
        r = self.client.post(
            "/notification-zones",
            {
                "name": "Pump Area",
                "description": "tank + pump",
                "sensors": [
                    {"sensor_key": "temperature_weather", "source_zone": self.zone.id}
                ],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["name"], "Pump Area")
        self.assertEqual(len(body["sensors"]), 1)
        self.assertEqual(body["sensors"][0]["source_zone"], self.zone.id)
        self.assertTrue(
            NotificationZoneSensor.objects.filter(
                notification_zone_id=body["id"], sensor_key="temperature_weather"
            ).exists()
        )

    def test_create_rejects_unowned_source_zone(self):
        other_zone = _zone(_user("bob"), name="bob-zone")
        r = self.client.post(
            "/notification-zones",
            {
                "name": "X",
                "sensors": [
                    {"sensor_key": "temperature_weather", "source_zone": other_zone.id}
                ],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)

    def test_create_rejects_unknown_sensor_key(self):
        r = self.client.post(
            "/notification-zones",
            {"name": "X", "sensors": [{"sensor_key": "bogus"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)

    def test_available_sensors_lists_zone_streams(self):
        TemperatureWeather.objects.create(
            zone=self.zone, user=self.user, timestamp=timezone.now(), value=21.0
        )
        r = self.client.get("/notification-zones/available-sensors")
        self.assertEqual(r.status_code, 200, r.content)
        zones = r.json()["zones"]
        mine = next(z for z in zones if z["zone_id"] == self.zone.id)
        keys = {s["sensor_key"] for s in mine["sensors"]}
        self.assertIn("temperature_weather", keys)

    def test_list_is_user_scoped(self):
        NotificationZone.objects.create(user=_user("bob"), name="bob-nz")
        NotificationZone.objects.create(user=self.user, name="mine")
        r = self.client.get("/notification-zones")
        self.assertEqual(r.status_code, 200)
        names = {z["name"] for z in r.json()}
        self.assertEqual(names, {"mine"})


class TestAlertNotificationZoneBinding(TestCase):
    def setUp(self):
        self.user = _user("alice")
        self.zone = _zone(self.user)
        self.nz = NotificationZone.objects.create(user=self.user, name="nz")
        self.client = _authed_client(self.user)

    def _payload(self, **kw):
        p = {
            "name": "Heat",
            "type": "Weather Temperature",
            "condition": ">",
            "condition_nbr": 30.0,
            "sensor_key": "temperature_weather",
        }
        p.update(kw)
        return p

    def test_create_alert_bound_to_notification_zone(self):
        r = self.client.post(
            "/alerts", self._payload(notification_zone=self.nz.id), format="json"
        )
        self.assertEqual(r.status_code, 201, r.content)
        alert = Alert.objects.get(id=r.json()["id"])
        self.assertEqual(alert.notification_zone_id, self.nz.id)
        self.assertIsNone(alert.zone_id)

    def test_rejects_zone_and_notification_zone_both(self):
        r = self.client.post(
            "/alerts",
            self._payload(zone=self.zone.id, notification_zone=self.nz.id),
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)

    def test_rejects_unowned_notification_zone(self):
        other_nz = NotificationZone.objects.create(user=_user("bob"), name="bob-nz")
        r = self.client.post(
            "/alerts", self._payload(notification_zone=other_nz.id), format="json"
        )
        self.assertEqual(r.status_code, 400, r.content)


class TestNotificationZoneDispatch(TestCase):
    def setUp(self):
        self.user = _user("alice")
        self.source_zone = _zone(self.user, name="source")
        self.other_zone = _zone(self.user, name="other")
        self.nz = NotificationZone.objects.create(user=self.user, name="nz")
        NotificationZoneSensor.objects.create(
            notification_zone=self.nz,
            sensor_key="temperature_weather",
            source_zone=self.source_zone,
        )
        self.alert = Alert.objects.create(
            name="Heat",
            type="Weather Temperature",
            description="",
            condition=">",
            condition_nbr=Decimal("30.00"),
            sensor_key="temperature_weather",
            user=self.user,
            notification_zone=self.nz,
            is_active=True,
        )

    def test_fires_for_assigned_source_zone(self):
        n = dispatch_alerts_for_reading(
            sensor_key="temperature_weather",
            zone=self.source_zone,
            user=self.user,
            value=35.0,
            timestamp=timezone.now(),
        )
        self.assertEqual(n, 1)

    def test_does_not_fire_for_other_zone(self):
        n = dispatch_alerts_for_reading(
            sensor_key="temperature_weather",
            zone=self.other_zone,
            user=self.user,
            value=35.0,
            timestamp=timezone.now(),
        )
        self.assertEqual(n, 0)

    @patch("agriapi.tasks.send_alert_sms.delay")
    def test_notify_sms_fans_out(self, mock_sms):
        self.alert.notify_sms = True
        self.alert.save(update_fields=["notify_sms"])
        dispatch_alerts_for_reading(
            sensor_key="temperature_weather",
            zone=self.source_zone,
            user=self.user,
            value=35.0,
            timestamp=timezone.now(),
        )
        self.assertTrue(mock_sms.called)
