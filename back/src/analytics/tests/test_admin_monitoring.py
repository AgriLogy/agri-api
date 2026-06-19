"""Tests for the admin monitoring endpoints (django-ninja)."""

import pytest

from apps.irrigation.models import (
    LoginEvent,
    NotificationDeliveryLog,
    TaskRun,
)

OVERVIEW_URL = "/admin/monitoring/overview"
TASKS_URL = "/admin/monitoring/tasks"
DELIVERIES_URL = "/admin/monitoring/deliveries"
LOGINS_URL = "/admin/monitoring/logins"


@pytest.mark.django_db
class TestAdminMonitoring:
    def test_normal_user_is_403(self, user_bearer):
        assert user_bearer.get(OVERVIEW_URL).status_code == 403
        assert user_bearer.get(TASKS_URL).status_code == 403
        assert user_bearer.get(DELIVERIES_URL).status_code == 403
        assert user_bearer.get(LOGINS_URL).status_code == 403

    def test_overview_aggregates(self, admin_bearer, normal_user):
        TaskRun.objects.create(task_name="agriapi.tasks.compute_et0_vpd_hourly")
        TaskRun.objects.create(
            task_name="agriapi.tasks.send_periodic_notifications",
            status=TaskRun.FAILURE,
            error="boom",
        )
        NotificationDeliveryLog.objects.create(
            channel="email", kind="periodic", recipient="a@b.c", status="sent"
        )
        NotificationDeliveryLog.objects.create(
            channel="sms", kind="outbound", recipient="+1", status="failed", error="x"
        )
        LoginEvent.objects.create(username=normal_user.username, success=True)
        LoginEvent.objects.create(username="ghost", success=False)

        resp = admin_bearer.get(OVERVIEW_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["tasks"]["total"] == 2
        assert body["tasks"]["failures"] == 1
        assert body["deliveries"]["total"] == 2
        assert body["deliveries"]["failed"] == 1
        assert body["logins"]["total"] == 2
        assert body["logins"]["failed"] == 1
        assert len(body["recent_failures"]) >= 2

    def test_tasks_history_and_schedule(self, admin_bearer):
        TaskRun.objects.create(task_name="agriapi.tasks.compute_et0_vpd_hourly")
        resp = admin_bearer.get(TASKS_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["runs"]) >= 1
        assert len(body["aggregates"]) >= 1
        # The static beat schedule should always be present in CI.
        assert any(e["source"] == "static" for e in body["schedule"])

    def test_tasks_filter_by_status(self, admin_bearer):
        TaskRun.objects.create(task_name="t.ok")
        TaskRun.objects.create(task_name="t.bad", status=TaskRun.FAILURE)
        runs = admin_bearer.get(f"{TASKS_URL}?status=failure").json()["runs"]
        assert runs and all(r["status"] == "failure" for r in runs)

    def test_deliveries_filter_by_channel(self, admin_bearer):
        NotificationDeliveryLog.objects.create(
            channel="email", recipient="a@b.c", status="sent"
        )
        NotificationDeliveryLog.objects.create(
            channel="whatsapp", recipient="+1", status="sent"
        )
        rows = admin_bearer.get(f"{DELIVERIES_URL}?channel=whatsapp").json()
        assert rows and all(r["channel"] == "whatsapp" for r in rows)

    def test_logins_filter_by_success(self, admin_bearer):
        LoginEvent.objects.create(username="u1", success=True)
        LoginEvent.objects.create(username="u2", success=False)
        rows = admin_bearer.get(f"{LOGINS_URL}?success=false").json()
        assert rows and all(r["success"] is False for r in rows)
