"""Feature 4: irrigation automation — programs CRUD, manual commands, scheduler.

Physical dispatch is simulation-only by default (IRRIGATION_DISPATCH_ENABLED is
False), so commands land in status ``simulated`` and never touch hardware.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils.timezone import now
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from analytics.models import IrrigationProgram, OutputCommand, Zone

User = get_user_model()


def _authed(user) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return c


@pytest.fixture
def user(db):
    u = User.objects.create(username="irr_owner", email="irr@e.com", is_active=True)
    u.set_password("pw")
    u.save()
    return u


@pytest.fixture
def zone(user):
    return Zone.objects.create(
        user=user, name="Plot", space=1000.0, critical_moisture_threshold=25.0
    )


# ── programs CRUD + manual command ───────────────────────────────────────────
@pytest.mark.django_db
class TestProgramsAndCommands:
    def test_program_crud_and_isolation(self, user, zone):
        c = _authed(user)
        r = c.post(
            "/irrigation/programs",
            {
                "name": "Morning",
                "zone_id": zone.id,
                "start_time": "06:30:00",
                "weekdays": "1,3,5",
                "duration_min": 20,
            },
            format="json",
        )
        assert r.status_code == 200, r.content
        pid = r.json()["id"]
        assert c.get("/irrigation/programs").json()[0]["name"] == "Morning"
        # update
        r = c.put(
            f"/irrigation/programs/{pid}",
            {
                "name": "Evening",
                "zone_id": zone.id,
                "start_time": "19:00:00",
                "enabled": False,
            },
            format="json",
        )
        assert r.json()["name"] == "Evening" and r.json()["enabled"] is False
        # another user can't see it
        other = User.objects.create(username="irr_other", email="o@e.com")
        assert _authed(other).get("/irrigation/programs").json() == []
        # delete
        assert c.delete(f"/irrigation/programs/{pid}").status_code == 200
        assert c.get("/irrigation/programs").json() == []

    def test_program_rejects_unowned_zone(self, user):
        other_zone = Zone.objects.create(
            user=User.objects.create(username="z_owner"),
            name="X",
            space=1.0,
            critical_moisture_threshold=25.0,
        )
        r = _authed(user).post(
            "/irrigation/programs",
            {"name": "x", "zone_id": other_zone.id, "start_time": "06:00:00"},
            format="json",
        )
        assert r.status_code == 404

    def test_manual_command_is_simulated_by_default(self, user, zone):
        r = _authed(user).post(
            "/irrigation/commands",
            {"zone_id": zone.id, "action": "open"},
            format="json",
        )
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["action"] == "open"
        assert body["status"] == "simulated"  # no hardware actuated
        assert body["dispatched_at"]
        assert _authed(user).get("/irrigation/commands").json()[0]["id"] == body["id"]

    def test_command_rejects_bad_action(self, user, zone):
        r = _authed(user).post(
            "/irrigation/commands",
            {"zone_id": zone.id, "action": "boom"},
            format="json",
        )
        assert r.status_code == 400

    def test_technician_blocked_from_writes(self, user, zone):
        user.is_technician = True
        user.save(update_fields=["is_technician"])
        c = _authed(user)
        assert (
            c.post(
                "/irrigation/programs",
                {"name": "x", "zone_id": zone.id, "start_time": "06:00:00"},
                format="json",
            ).status_code
            == 403
        )
        assert (
            c.post(
                "/irrigation/commands",
                {"zone_id": zone.id, "action": "open"},
                format="json",
            ).status_code
            == 403
        )

    def test_config_reports_simulation(self, user):
        assert _authed(user).get("/irrigation/config").json() == {
            "dispatch_enabled": False
        }


# ── scheduler ────────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestScheduler:
    def _due_program(self, user, zone, **over):
        fields = {
            "user": user,
            "zone": zone,
            "name": "Due",
            "enabled": True,
            "start_time": (now() - timedelta(minutes=1)).time(),
            "weekdays": "",
        }
        fields.update(over)
        return IrrigationProgram.objects.create(**fields)

    def test_due_program_fires_once_then_dedups(self, user, zone):
        from agriapi.tasks import run_due_irrigation_programs

        self._due_program(user, zone)
        r1 = run_due_irrigation_programs()
        r2 = run_due_irrigation_programs()  # same window → deduped
        assert r1["fired"] == 1
        assert r2["fired"] == 0
        cmds = OutputCommand.objects.filter(source=OutputCommand.SCHEDULED)
        assert cmds.count() == 1
        assert cmds.first().status == "simulated"

    def test_disabled_and_wrong_weekday_skipped(self, user, zone):
        from agriapi.tasks import run_due_irrigation_programs

        self._due_program(user, zone, enabled=False)
        # a weekday that is definitely not today
        not_today = str(((now().isoweekday()) % 7) + 1)
        self._due_program(user, zone, name="WrongDay", weekdays=not_today)
        r = run_due_irrigation_programs()
        assert r["fired"] == 0
        assert not OutputCommand.objects.exists()
