"""Tests for the admin records endpoints (notifications / conversations /
proactive notices / technician grants) — django-ninja."""

from datetime import date, time

import pytest
from django.utils import timezone

from apps.alerts.models import Notification
from apps.assistant.models import AssistantConversation, ProactiveNotice
from apps.irrigation.models import TechnicianGrant

NOTIF_URL = "/admin/notifications"
CONV_URL = "/admin/conversations"
PROACTIVE_URL = "/admin/proactive-notices"
GRANTS_URL = "/admin/technician-grants"


def _make_notification(user):
    return Notification.objects.create(
        user=user,
        yesterday_temperature=20,
        today_temperature=22,
        yesterday_humidity=50,
        today_humidity=55,
        ET0=4,
        soil_humidity=30,
        soil_temperature=18,
        soil_ph=7,
        perfect_irrigation_period="06:00-08:00",
        last_irrigation_date=date(2026, 1, 1),
        last_start_irrigation_hour=time(6, 0),
        last_finish_irrigation_hour=time(8, 0),
        used_water_irrigation=100,
    )


def _make_conversation(user, client_id="c1", title="Hi"):
    now = timezone.now()
    return AssistantConversation.objects.create(
        user=user,
        client_id=client_id,
        title=title,
        messages=[{"role": "user", "content": "hello"}],
        created_at=now,
        updated_at=now,
    )


@pytest.mark.django_db
class TestAdminRecords:
    def test_all_endpoints_403_for_normal_user(self, user_bearer):
        assert user_bearer.get(NOTIF_URL).status_code == 403
        assert user_bearer.get(CONV_URL).status_code == 403
        assert user_bearer.get(PROACTIVE_URL).status_code == 403
        assert user_bearer.get(GRANTS_URL).status_code == 403

    def test_notifications_list_and_delete(self, admin_bearer, normal_user):
        n = _make_notification(normal_user)
        rows = admin_bearer.get(NOTIF_URL).json()
        assert any(r["id"] == n.id for r in rows)
        assert any(r["username"] == normal_user.username for r in rows)
        # filter
        filtered = admin_bearer.get(
            f"{NOTIF_URL}?username={normal_user.username}"
        ).json()
        assert filtered and all(r["username"] == normal_user.username for r in filtered)
        assert admin_bearer.delete(f"{NOTIF_URL}/{n.id}").status_code == 200
        assert not Notification.objects.filter(id=n.id).exists()

    def test_conversations_list_detail_delete(self, admin_bearer, normal_user):
        c = _make_conversation(normal_user)
        rows = admin_bearer.get(CONV_URL).json()
        row = next(r for r in rows if r["id"] == c.id)
        assert row["message_count"] == 1
        assert "messages" not in row  # list omits messages

        detail = admin_bearer.get(f"{CONV_URL}/{c.id}").json()
        assert detail["messages"] == [{"role": "user", "content": "hello"}]

        assert admin_bearer.get(f"{CONV_URL}/999999").status_code == 404
        assert admin_bearer.delete(f"{CONV_URL}/{c.id}").status_code == 200
        assert not AssistantConversation.objects.filter(id=c.id).exists()

    def test_proactive_list_and_reset(self, admin_bearer, normal_user):
        p = ProactiveNotice.objects.create(user=normal_user, last_sent=timezone.now())
        rows = admin_bearer.get(PROACTIVE_URL).json()
        assert any(r["id"] == p.id for r in rows)
        assert admin_bearer.delete(f"{PROACTIVE_URL}/{p.id}").status_code == 200
        assert not ProactiveNotice.objects.filter(id=p.id).exists()

    def test_grants_list_patch_revoke(self, admin_bearer, normal_user, other_user):
        g = TechnicianGrant.objects.create(
            technician=other_user, owner=normal_user, is_active=True
        )
        rows = admin_bearer.get(GRANTS_URL).json()
        row = next(r for r in rows if r["id"] == g.id)
        assert row["owner"] == normal_user.username
        assert row["technician"] == other_user.username

        # filter by owner
        filtered = admin_bearer.get(f"{GRANTS_URL}?owner={normal_user.username}").json()
        assert filtered and all(r["owner"] == normal_user.username for r in filtered)

        # detail includes scope
        detail = admin_bearer.get(f"{GRANTS_URL}/{g.id}").json()
        assert "scope" in detail

        # toggle is_active
        patched = admin_bearer.patch(
            f"{GRANTS_URL}/{g.id}", {"is_active": False}, format="json"
        )
        assert patched.status_code == 200
        assert patched.json()["is_active"] is False

        # revoke
        assert admin_bearer.delete(f"{GRANTS_URL}/{g.id}").status_code == 200
        assert not TechnicianGrant.objects.filter(id=g.id).exists()
