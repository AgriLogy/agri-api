"""Email + notification surface tests (django-ninja, JWT bearer auth).

Covers:
  - POST /users/me/notifications delivers to the caller's email and bumps
    last_notified; rejects callers without an email; requires auth.
  - POST /notifications/zone-outbound sends only when channels.email is True
    and accepts a contactEmail override; no-ops on non-email channels;
    rejects when no recipient resolves; requires auth.
  - GET /notifications returns the caller's own rows in the documented shape
    and excludes other users' rows.
  - send_periodic_notifications iterates active users, gates on should_notify,
    and updates last_notified.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone

from agriapi.tasks import send_periodic_notifications
from analytics.models import Notification

# The send-notification flow composes via field_snapshot, which now delegates
# to agri-core's DB-backed handler (own SQLAlchemy connection, AGRI_DB_URL).
# Those tests need a shared Postgres + a committed transaction so the separate
# connection sees the data. They skip on sqlite (fast local dev); CI runs them
# on Postgres.
_requires_pg = pytest.mark.skipif(
    not settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM field_snapshot requires Postgres",
)

SEND_NOTIFICATION_URL = "/users/me/notifications"
ZONE_OUTBOUND_URL = "/notifications/zone-outbound"
NOTIFICATIONS_URL = "/notifications"


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


@pytest.mark.django_db
class TestSendNotificationEmail:
    @_requires_pg
    @pytest.mark.django_db(transaction=True)
    def test_sends_to_caller_email(self, user_bearer, normal_user):
        r = user_bearer.post(SEND_NOTIFICATION_URL)
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [normal_user.email]

    @_requires_pg
    @pytest.mark.django_db(transaction=True)
    def test_bumps_last_notified(self, user_bearer, normal_user):
        assert normal_user.last_notified is None
        before = timezone.now()
        r = user_bearer.post(SEND_NOTIFICATION_URL)
        assert r.status_code == 200
        normal_user.refresh_from_db()
        assert normal_user.last_notified is not None
        assert normal_user.last_notified >= before

    def test_400_when_user_has_no_email(self, user_bearer, normal_user):
        type(normal_user).objects.filter(pk=normal_user.pk).update(email="")
        r = user_bearer.post(SEND_NOTIFICATION_URL)
        assert r.status_code == 400
        assert r.json()["success"] is False
        assert len(mail.outbox) == 0

    def test_unauthenticated_returns_401(self, anon_client):
        r = anon_client.post(SEND_NOTIFICATION_URL)
        assert r.status_code == 401


@pytest.mark.django_db
class TestZoneNotificationOutbound:
    def _payload(self, **overrides):
        payload = {
            "zoneId": 7,
            "subject": "Agrilogy — config",
            "message": "Config saved.",
            "channels": {"email": True, "sms": False, "whatsapp": False},
        }
        payload.update(overrides)
        return payload

    def test_sends_email_when_channel_enabled(self, user_bearer, normal_user):
        r = user_bearer.post(ZONE_OUTBOUND_URL, self._payload(), format="json")
        assert r.status_code == 202
        assert r.json() == {"status": "queued", "channels": ["email"]}
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [normal_user.email]
        assert mail.outbox[0].subject == "Agrilogy — config"

    def test_honours_contact_email_override(self, user_bearer):
        r = user_bearer.post(
            ZONE_OUTBOUND_URL,
            self._payload(contactEmail="ops@example.com"),
            format="json",
        )
        assert r.status_code == 202
        assert mail.outbox[0].to == ["ops@example.com"]

    def test_no_op_when_no_channels(self, user_bearer):
        r = user_bearer.post(
            ZONE_OUTBOUND_URL,
            self._payload(channels={"email": False, "sms": False, "whatsapp": False}),
            format="json",
        )
        assert r.status_code == 202
        assert r.json() == {"status": "noop"}
        assert len(mail.outbox) == 0

    def test_400_when_recipient_missing(self, user_bearer, normal_user):
        type(normal_user).objects.filter(pk=normal_user.pk).update(email="")
        r = user_bearer.post(ZONE_OUTBOUND_URL, self._payload(), format="json")
        assert r.status_code == 400
        assert len(mail.outbox) == 0

    def test_sends_sms_when_channel_enabled(self, user_bearer):
        with mock.patch("agriapi.twilio_messaging.send_sms", return_value=True) as m:
            r = user_bearer.post(
                ZONE_OUTBOUND_URL,
                self._payload(
                    channels={"email": False, "sms": True, "whatsapp": False},
                    contactPhone="+212600000000",
                ),
                format="json",
            )
        assert r.status_code == 202
        assert r.json() == {"status": "queued", "channels": ["sms"]}
        m.assert_called_once()
        assert m.call_args.args[0] == "+212600000000"

    def test_sends_whatsapp_when_channel_enabled(self, user_bearer):
        with mock.patch(
            "agriapi.twilio_messaging.send_whatsapp", return_value=True
        ) as m:
            r = user_bearer.post(
                ZONE_OUTBOUND_URL,
                self._payload(
                    channels={"email": False, "sms": False, "whatsapp": True},
                    contactPhone="+212600000000",
                ),
                format="json",
            )
        assert r.status_code == 202
        assert r.json() == {"status": "queued", "channels": ["whatsapp"]}
        m.assert_called_once()

    def test_email_and_sms_together(self, user_bearer, normal_user):
        with mock.patch("agriapi.twilio_messaging.send_sms", return_value=True) as m:
            r = user_bearer.post(
                ZONE_OUTBOUND_URL,
                self._payload(
                    channels={"email": True, "sms": True, "whatsapp": False},
                    contactPhone="+212600000000",
                ),
                format="json",
            )
        assert r.status_code == 202
        assert r.json() == {"status": "queued", "channels": ["email", "sms"]}
        assert len(mail.outbox) == 1
        m.assert_called_once()

    def test_400_when_phone_missing_for_sms(self, user_bearer, normal_user):
        type(normal_user).objects.filter(pk=normal_user.pk).update(phone_number=None)
        r = user_bearer.post(
            ZONE_OUTBOUND_URL,
            self._payload(channels={"email": False, "sms": True, "whatsapp": False}),
            format="json",
        )
        assert r.status_code == 400

    def test_unauthenticated_returns_401(self, anon_client):
        r = anon_client.post(ZONE_OUTBOUND_URL, self._payload(), format="json")
        assert r.status_code == 401


@pytest.mark.django_db
class TestNotificationsAndAlerts:
    def test_returns_caller_rows_only(self, user_bearer, normal_user, other_user):
        _seed_notification(normal_user)
        _seed_notification(normal_user)
        _seed_notification(other_user)

        r = user_bearer.get(NOTIFICATIONS_URL)
        assert r.status_code == 200
        body = r.json()
        assert "notifications" in body
        assert len(body["notifications"]) == 2

    def test_response_shape_matches_frontend_expectation(
        self, user_bearer, normal_user
    ):
        _seed_notification(normal_user)
        r = user_bearer.get(NOTIFICATIONS_URL)
        assert r.status_code == 200
        row = r.json()["notifications"][0]
        for key in ("id", "is_read", "read_at", "zone_name", "notification"):
            assert key in row
        for nested in (
            "yesterday_temperature",
            "today_temperature",
            "ET0",
            "soil_humidity",
            "notification_date",
        ):
            assert nested in row["notification"]

    def test_returns_empty_list_when_no_rows(self, user_bearer):
        r = user_bearer.get(NOTIFICATIONS_URL)
        assert r.status_code == 200
        assert r.json() == {"notifications": []}


@_requires_pg
@pytest.mark.django_db(transaction=True)
class TestSendPeriodicNotificationsTask:
    def _make_user(self, username, email="x@example.com", **extra):
        from django.contrib.auth import get_user_model

        User = get_user_model()
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

    def test_iterates_users_and_sends(self):
        u1 = self._make_user("u1", email="u1@example.com")
        u2 = self._make_user("u2", email="u2@example.com")

        result = send_periodic_notifications()

        assert result["sent"] == 2
        assert result["skipped"] == 0
        assert result["failed"] == 0
        assert len(mail.outbox) == 2

        for u in (u1, u2):
            u.refresh_from_db()
            assert u.last_notified is not None

    def test_skips_users_without_email(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        u_with = self._make_user("with-email", email="with@example.com")
        User.objects.filter(pk=u_with.pk).update(last_notified=None)

        u_no = self._make_user("no-email", email="placeholder@example.com")
        User.objects.filter(pk=u_no.pk).update(email="")

        result = send_periodic_notifications()

        assert result["sent"] == 1
        assert result["skipped"] == 1
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [u_with.email]

    def test_skips_users_within_cadence(self):
        u = self._make_user("recent", email="recent@example.com")
        u.notify_every = 240  # minutes (4 h)
        u.last_notified = timezone.now() - timedelta(minutes=10)
        u.save()

        result = send_periodic_notifications()

        assert result["sent"] == 0
        assert result["skipped"] == 1
        assert len(mail.outbox) == 0

    def test_skips_inactive_users(self):
        self._make_user("dormant", email="dormant@example.com", is_active=False)
        result = send_periodic_notifications()
        assert result["sent"] == 0
        assert len(mail.outbox) == 0

    def test_failed_send_still_advances_last_notified(self):
        # A persistent provider failure must not leave the user "due" forever:
        # last_notified is advanced on failure too, so the next beat tick skips
        # them (cadence respected) instead of re-attempting every tick.
        u = self._make_user("flaky", email="flaky@example.com")
        before = timezone.now()

        with (
            mock.patch("agriapi.tasks.perform_calculations", return_value="msg"),
            mock.patch(
                "agriapi.tasks.send_mail", side_effect=Exception("provider down")
            ),
        ):
            result = send_periodic_notifications()

        assert result["sent"] == 0
        assert result["failed"] == 1
        assert len(mail.outbox) == 0

        u.refresh_from_db()
        assert u.last_notified is not None
        assert u.last_notified >= before

    def test_atomic_claim_prevents_double_send(self):
        # Two consecutive beat runs (simulating the concurrent-beat race) must
        # send the digest exactly ONCE: the first run's atomic claim stamps
        # last_notified, so the second run's conditional UPDATE matches no row
        # and the user is skipped. Without the claim the user would be re-sent.
        u = self._make_user("racer", email="racer@example.com")

        first = send_periodic_notifications()
        second = send_periodic_notifications()

        assert first["sent"] == 1
        assert second["sent"] == 0
        assert second["skipped"] == 1
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [u.email]

    def test_claim_helper_only_one_winner(self):
        # The claim helper itself: the first call for a due user wins (True),
        # an immediate second call loses (False) because the cadence window was
        # just reset by the first claim.
        from apps.users.notification_helper import claim_notification_slot

        u = self._make_user("claimant", email="claimant@example.com")

        assert claim_notification_slot(u) is True
        assert claim_notification_slot(u) is False
