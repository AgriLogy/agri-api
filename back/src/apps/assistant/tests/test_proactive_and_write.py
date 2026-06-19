"""Feature 3: assistant write-tools + proactive-insight beat task."""

from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail

from apps.assistant.tools import _create_alert, _set_notification_cadence


def _user(**over):
    fields = {
        "username": "f3_owner",
        "email": "f3@example.com",
        "password": "pw",
    }
    fields.update(over)
    return get_user_model().objects.create_user(**fields)


# ── write tools ──────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestWriteTools:
    def test_create_alert_persists_for_caller(self):
        user = _user()
        out = _create_alert(
            user,
            {
                "name": "Dry soil",
                "sensor_key": "soilMoisture",
                "condition": "<",
                "condition_nbr": 20,
            },
        )
        created = out["created"]
        assert created["id"] and created["sensor_key"] == "soilMoisture"
        from apps.alerts.models import Alert

        alert = Alert.objects.get(pk=created["id"])
        assert alert.user_id == user.id and alert.is_active

    @pytest.mark.parametrize(
        "params",
        [
            {
                "name": "",
                "sensor_key": "soilMoisture",
                "condition": "<",
                "condition_nbr": 1,
            },
            {
                "name": "x",
                "sensor_key": "soilMoisture",
                "condition": "!",
                "condition_nbr": 1,
            },
            {"name": "x", "sensor_key": "nope", "condition": "<", "condition_nbr": 1},
            {"name": "x", "sensor_key": "soilMoisture", "condition": "<"},
        ],
    )
    def test_create_alert_validates(self, params):
        out = _create_alert(_user(), params)
        assert out.get("created") is None and "error" in out

    def test_create_alert_blocks_technician(self):
        user = _user(username="f3_tech")
        user.is_technician = True
        out = _create_alert(
            user,
            {
                "name": "x",
                "sensor_key": "soilMoisture",
                "condition": "<",
                "condition_nbr": 1,
            },
        )
        assert out["created"] is None and "Technician" in out["error"]

    def test_set_cadence_updates_and_floors(self):
        user = _user(username="f3_cad")
        assert _set_notification_cadence(user, {"minutes": 30})["notify_every"] == 30
        user.refresh_from_db()
        assert user.notify_every == 30
        assert "error" in _set_notification_cadence(user, {"minutes": 5})


# ── proactive scan task ──────────────────────────────────────────────────────
@pytest.mark.django_db
class TestProactiveScan:
    def _run(self):
        from agriapi.tasks import scan_proactive_insights

        return scan_proactive_insights()

    def test_emails_once_per_cooldown(self):
        _user(username="p1", email="p1@example.com")
        advice = {
            "recommendation": "irrigate",
            "zone_name": "Zone 1",
            "reason": "Sol sec.",
            "estimated_water_m3": 12.0,
        }
        with mock.patch(
            "apps.assistant.tools._get_irrigation_advice", return_value=advice
        ):
            r1 = self._run()
            r2 = self._run()  # second tick within cooldown → deduped
        assert r1["notified"] == 1
        assert r2["notified"] == 0  # claim already held
        assert len(mail.outbox) == 1
        assert "Zone 1" in mail.outbox[0].subject

    def test_hold_recommendation_does_not_email(self):
        _user(username="p2", email="p2@example.com")
        with mock.patch(
            "apps.assistant.tools._get_irrigation_advice",
            return_value={"recommendation": "hold"},
        ):
            r = self._run()
        assert r["notified"] == 0 and r["quiet"] == 1 and not mail.outbox

    def test_skips_staff_and_technicians(self):
        _user(username="p_staff", email="s@example.com", is_staff=True)
        tech = _user(username="p_tech", email="t@example.com")
        tech.is_technician = True
        tech.save(update_fields=["is_technician"])
        with mock.patch(
            "apps.assistant.tools._get_irrigation_advice",
            return_value={"recommendation": "irrigate", "zone_name": "Z", "reason": ""},
        ):
            r = self._run()
        assert r["scanned"] == 0 and not mail.outbox
