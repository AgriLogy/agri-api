"""Email + notification surface tests.

Covers:
  - SendNotificationEmailView delivers to request.user.email and bumps
    last_notified.
  - ZoneNotificationOutboundAPIView sends only when channels.email is True
    and accepts contactEmail override; no-ops on non-email channels.
  - NotificationsAndAlertsAPIView returns the user's own rows in the
    documented shape and excludes other users' rows.
  - send_periodic_notifications iterates active users, gates on
    should_notify, and updates last_notified.

Run:
    SECRET_KEY=k DEBUG=True ALLOWED_HOSTS=* USE_POSTGRES=False \
        uv run python manage.py test agriBack.tests
"""

from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from agriBack.tasks import send_periodic_notifications
from analytics.models import Notification

User = get_user_model()


def _make_user(username="alice", email="alice@example.com", **extra):
    defaults = {
        "email": email,
        "firstname": "Alice",
        "lastname": "Doe",
        "is_active": True,
    }
    defaults.update(extra)
    user = User.objects.create(username=username, **defaults)
    user.set_password("pw")
    user.save()
    return user


def _seed_notification(user, **overrides):
    now = timezone.now()
    payload = {
        "user": user,
        "yesterday_temperature": Decimal("22.50"),
        "today_temperature": Decimal("24.00"),
        "yesterday_humidity": Decimal("60.00"),
        "today_humidity": Decimal("55.00"),
        "ET0": Decimal("3.20"),
        "soil_humidity": Decimal("28.00"),
        "soil_temperature": Decimal("21.00"),
        "soil_ph": Decimal("6.80"),
        "perfect_irrigation_period": "06:00-07:00",
        "last_irrigation_date": now.date(),
        "last_start_irrigation_hour": time(6, 0),
        "last_finish_irrigation_hour": time(7, 0),
        "used_water_irrigation": Decimal("500.00"),
        "notification_date": now,
    }
    payload.update(overrides)
    return Notification.objects.create(**payload)


class SendNotificationEmailViewTests(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_sends_to_request_user_email(self):
        r = self.client.get(reverse("send-notification"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])

    def test_bumps_last_notified(self):
        self.assertIsNone(self.user.last_notified)
        before = timezone.now()
        self.client.get(reverse("send-notification"))
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_notified)
        self.assertGreaterEqual(self.user.last_notified, before)

    def test_400_when_user_has_no_email(self):
        self.user.email = ""
        self.user.save()
        r = self.client.get(reverse("send-notification"))
        # CustomUser.email is unique=True but we allow runtime-blank for the test;
        # the view should reject empty email.
        # If save() fails because of DB constraints, fall back to overriding the field.
        self.assertIn(r.status_code, (400,))

    def test_unauthenticated_returns_401(self):
        client = APIClient()
        r = client.get(reverse("send-notification"))
        self.assertEqual(r.status_code, 401)


class ZoneNotificationOutboundTests(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = "/api/zone-notification-outbound/"

    def _payload(self, **overrides):
        payload = {
            "zoneId": 7,
            "subject": "Agrilogy — config",
            "message": "Config saved.",
            "channels": {"email": True, "sms": False, "whatsapp": False},
        }
        payload.update(overrides)
        return payload

    def test_sends_email_when_channel_enabled(self):
        r = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data, {"status": "sent"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertEqual(mail.outbox[0].subject, "Agrilogy — config")

    def test_honours_contact_email_override(self):
        r = self.client.post(
            self.url,
            self._payload(contactEmail="ops@example.com"),
            format="json",
        )
        self.assertEqual(r.status_code, 202)
        self.assertEqual(mail.outbox[0].to, ["ops@example.com"])

    def test_no_op_when_no_channels(self):
        r = self.client.post(
            self.url,
            self._payload(channels={"email": False, "sms": True, "whatsapp": False}),
            format="json",
        )
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data, {"status": "noop"})
        self.assertEqual(len(mail.outbox), 0)

    def test_400_when_recipient_missing(self):
        self.user.email = ""
        self.user.save()
        r = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_unauthenticated_returns_401(self):
        client = APIClient()
        r = client.post(self.url, self._payload(), format="json")
        self.assertEqual(r.status_code, 401)


class NotificationsAndAlertsTests(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.other = _make_user(username="bob", email="bob@example.com")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_returns_user_rows_only(self):
        _seed_notification(self.user)
        _seed_notification(self.user)
        _seed_notification(self.other)

        r = self.client.get("/api/notifications-and-alerts/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("notifications", body)
        self.assertEqual(len(body["notifications"]), 2)

    def test_response_shape_matches_frontend_expectation(self):
        _seed_notification(self.user)
        r = self.client.get("/api/notifications-and-alerts/")
        row = r.json()["notifications"][0]
        for key in ("id", "is_read", "read_at", "zone_name", "notification"):
            self.assertIn(key, row)
        for nested in (
            "yesterday_temperature",
            "today_temperature",
            "ET0",
            "soil_humidity",
            "notification_date",
        ):
            self.assertIn(nested, row["notification"])

    def test_returns_empty_list_when_no_rows(self):
        r = self.client.get("/api/notifications-and-alerts/")
        self.assertEqual(r.json(), {"notifications": []})


class SendPeriodicNotificationsTaskTests(TestCase):
    def test_iterates_users_and_sends(self):
        u1 = _make_user(username="u1", email="u1@example.com")
        u2 = _make_user(username="u2", email="u2@example.com")

        result = send_periodic_notifications()

        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(mail.outbox), 2)

        for u in (u1, u2):
            u.refresh_from_db()
            self.assertIsNotNone(u.last_notified)

    def test_skips_users_without_email(self):
        # CustomUser.email is unique + non-null, but blank is allowed at the
        # ORM layer. Force one user to a blank email post-create.
        u_with = _make_user(username="with-email", email="with@example.com")
        User.objects.filter(pk=u_with.pk).update(last_notified=None)

        u_no = _make_user(username="no-email", email="placeholder@example.com")
        User.objects.filter(pk=u_no.pk).update(email="")

        result = send_periodic_notifications()

        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [u_with.email])

    def test_skips_users_within_cadence(self):
        u = _make_user(username="recent")
        u.notify_every = 4  # hours
        u.last_notified = timezone.now() - timedelta(minutes=10)
        u.save()

        result = send_periodic_notifications()

        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_inactive_users(self):
        _make_user(username="dormant", is_active=False)
        result = send_periodic_notifications()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(len(mail.outbox), 0)
