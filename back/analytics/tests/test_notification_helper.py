"""
Unit tests for CustomUser.notification_helper.

The end-to-end paths are exercised in test_email_notifications.py; this
file pins the small pure-ish helpers (should_notify, _format_message,
perform_calculations) so they can evolve independently without breaking
the email template contract.
"""

from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from CustomUser.notification_helper import (
    _format_message,
    perform_calculations,
    should_notify,
)

User = get_user_model()


def _u(**extra):
    payload = {
        "username": "alice",
        "email": "alice@example.com",
        "firstname": "Alice",
        "lastname": "Doe",
        "is_active": True,
    }
    payload.update(extra)
    user = User.objects.create(**payload)
    user.set_password("pw")
    user.save()
    return user


SAMPLE_SNAPSHOT = {
    "zone_name": "zone-1",
    "date_today": date(2026, 5, 9),
    "yesterday_temp_c": 21.5,
    "today_temp_c": 24.0,
    "yesterday_humidity_pct": 60.0,
    "today_humidity_pct": 55.0,
    "et0_today_mm": 3.21,
    "soil_moisture_pct": 18.0,
    "soil_temperature_c": 22.0,
    "soil_ph": 6.7,
    "soil_ec": 800.0,
    "soil_salinity": 1500.0,
    "npk_n": 80.0,
    "npk_p": 40.0,
    "npk_k": 120.0,
    "last_irrigation_at": timezone.now() - timedelta(hours=4),
    "last_irrigation_l": 12000.0,
    "perfect_irrigation_window": "06:00 – 07:00",
    "kc_used": 1.0,
    "irrigation_decision": "Irrigation recommandée maintenant.",
}


class ShouldNotifyTests(TestCase):
    def test_first_run_always_notifies(self):
        u = _u()
        self.assertIsNone(u.last_notified)
        self.assertTrue(should_notify(u))

    def test_within_cadence_does_not_notify(self):
        u = _u(notify_every=4)
        u.last_notified = timezone.now() - timedelta(hours=1)
        u.save()
        self.assertFalse(should_notify(u))

    def test_past_cadence_does_notify(self):
        u = _u(notify_every=4)
        u.last_notified = timezone.now() - timedelta(hours=5)
        u.save()
        self.assertTrue(should_notify(u))

    def test_default_cadence_when_attribute_missing(self):
        # When the user model has no notify_every attribute, the helper
        # should fall back to a 4h cadence rather than crash.
        class FakeUser:
            last_notified = timezone.now() - timedelta(hours=3)

        # 3h elapsed, default cadence 4h => not yet
        self.assertFalse(should_notify(FakeUser()))

        FakeUser.last_notified = timezone.now() - timedelta(hours=5)
        self.assertTrue(should_notify(FakeUser()))


class FormatMessageTests(TestCase):
    def test_includes_user_name_and_zone(self):
        u = _u(firstname="Marc")
        body = _format_message(u, SAMPLE_SNAPSHOT)
        self.assertIn("Bonjour Marc", body)
        self.assertIn("zone-1", body)
        self.assertIn("09/05/2026", body)

    def test_renders_em_dash_for_missing_values(self):
        snap = dict(SAMPLE_SNAPSHOT)
        snap.update(
            today_temp_c=None,
            yesterday_temp_c=None,
            soil_ph=None,
            npk_n=None,
            last_irrigation_at=None,
            last_irrigation_l=None,
        )
        body = _format_message(_u(), snap)
        self.assertIn("—", body)

    def test_includes_irrigation_decision(self):
        body = _format_message(_u(), SAMPLE_SNAPSHOT)
        self.assertIn("Irrigation recommandée maintenant.", body)

    def test_uses_username_when_firstname_missing(self):
        u = _u(firstname="")
        body = _format_message(u, SAMPLE_SNAPSHOT)
        self.assertIn("Bonjour alice", body)


class PerformCalculationsTests(TestCase):
    def test_calls_field_snapshot_and_renders(self):
        u = _u()
        with patch(
            "CustomUser.notification_helper.field_snapshot",
            return_value=SAMPLE_SNAPSHOT,
        ) as m:
            body = perform_calculations(u)
        m.assert_called_once_with(u)
        self.assertIn("zone-1", body)
        self.assertIn("ET0 cumulée", body)
