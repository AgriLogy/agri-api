"""Water level monitoring (agrilogy-front#261): sources, simulator, readings,
status, history, alerts, offline sweep and alert email.

The four ``water_*`` tables have no Django model; conftest creates them from
the agri.db definitions (Postgres only), and ``_db_env`` empties them after
each test. The basin used throughout is the developer brief's example:
8 x 3 m, 4.80 m max, 10 m³ reserve → at 3.62 m: 86.88 m³, 75.4 %.
"""

from __future__ import annotations

import datetime

import pytest
from django.conf import settings as dj_settings
from django.db import connection
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp import celery as fastapp_celery
from fastapp import tasks_water
from fastapp import water_level as svc
from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="water level tables are created on Postgres only",
)
pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

BASIN = {
    "name": "Main Basin",
    "source_type": "BASIN",
    "geometry": "RECTANGULAR",
    "max_height_m": 4.8,
    "length_m": 8,
    "width_m": 3,
    "minimum_reserve_m3": 10,
    "zone_ids": [3, 1, 3],
}


@pytest.fixture
def enqueued(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        fastapp_celery, "send_task", lambda name, **kw: calls.append((name, kw))
    )
    return calls


@pytest.fixture(autouse=True)
def _db_env(monkeypatch, enqueued):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)
    yield
    with connection.cursor() as cursor:  # no Django model → never flushed
        cursor.execute(
            "TRUNCATE water_source, water_level_device, water_level_reading, "
            "water_level_alert"
        )


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


def _user(django_user_model, name, **extra):
    return django_user_model.objects.create_user(
        username=name, email=f"{name}@example.com", password="irrelevant-3921", **extra
    )


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


def _create(fast, user, **overrides) -> dict:
    r = fast.post("/water-sources", json={**BASIN, **overrides}, headers=_auth(user))
    assert r.status_code == 201, r.text
    return r.json()


def _backdate_readings(source_id: int, minutes: float) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE water_level_reading SET measured_at = measured_at - %s "
            "WHERE water_source_id = %s",
            [datetime.timedelta(minutes=minutes), source_id],
        )


class TestSources:
    def test_create_get_list(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u)
        assert src["zone_ids"] == [1, 3]
        assert src["capacity_m3"] == pytest.approx(115.2)
        assert src["mode"] == "SIMULATED"
        assert [d["adapter"] for d in src["devices"]] == ["simulator"]

        r = fast.get(f"/water-sources/{src['id']}", headers=_auth(u))
        assert r.status_code == 200 and r.json()["name"] == "Main Basin"

        listed = fast.get("/water-sources", headers=_auth(u)).json()
        assert [s["id"] for s in listed] == [src["id"]]
        assert listed[0]["status"]["data_status"] == "OFFLINE"

    @pytest.mark.parametrize(
        "bad",
        [
            {"length_m": None},
            {"geometry": "IRREGULAR"},
            {"critical_pct": 50},
            {"max_height_m": 0},
            {"source_type": "LAKE"},
            {
                "geometry": "IRREGULAR",
                "calibration_table": [{"level_m": 1, "volume_m3": 1}],
            },
        ],
    )
    def test_create_rejects_uncomputable_source(self, fast, django_user_model, bad):
        u = _user(django_user_model, "wl-a")
        r = fast.post("/water-sources", json={**BASIN, **bad}, headers=_auth(u))
        assert r.status_code in (400, 422), r.text

    def test_irregular_and_well(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        pond = _create(
            fast,
            u,
            geometry="IRREGULAR",
            length_m=None,
            width_m=None,
            max_height_m=2,
            max_capacity_m3=500,
            calibration_table=[
                {"level_m": 2, "volume_m3": 500},
                {"level_m": 0, "volume_m3": 0},
            ],
        )
        assert pond["calibration_table"][0] == {"level_m": 0.0, "volume_m3": 0.0}
        well = _create(
            fast,
            u,
            source_type="WELL",
            geometry="CYLINDRICAL",
            length_m=None,
            width_m=None,
            max_height_m=30,
            total_depth_m=80,
        )
        r = fast.post(
            f"/water-sources/{well['id']}/simulate",
            json={"level_m": 12},
            headers=_auth(u),
        )
        m = r.json()["metrics"]
        assert m["depth_to_water_m"] == pytest.approx(68)
        assert m["volume_m3"] is None

    def test_owner_scoping(self, fast, django_user_model):
        a = _user(django_user_model, "wl-a")
        b = _user(django_user_model, "wl-b")
        src = _create(fast, a)
        for method, path, body in (
            ("get", f"/water-sources/{src['id']}", None),
            ("get", f"/water-sources/{src['id']}/status", None),
            ("get", f"/water-sources/{src['id']}/history", None),
            ("patch", f"/water-sources/{src['id']}", {"name": "x"}),
            ("post", f"/water-sources/{src['id']}/simulate", {"level_m": 1}),
            ("delete", f"/water-sources/{src['id']}", None),
        ):
            r = fast.request(method, path, json=body, headers=_auth(b))
            assert r.status_code == 404, (method, path, r.text)
        assert fast.get("/water-sources", headers=_auth(b)).json() == []
        r = fast.post(
            "/water-level/readings",
            json={"water_source_id": src["id"], "level_m": 1},
            headers=_auth(b),
        )
        assert r.status_code == 404

    def test_technician_is_read_only(self, fast, django_user_model):
        tech = _user(django_user_model, "wl-tech", is_technician=True)
        r = fast.post("/water-sources", json=BASIN, headers=_auth(tech))
        assert r.status_code == 403

    def test_patch_and_live_device(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u)
        r = fast.patch(
            f"/water-sources/{src['id']}",
            json={
                "high_pct": 90,
                "mode": "LIVE",
                "live_device": {"dev_eui": "A84041000181C9F1", "scale": 0.01},
            },
            headers=_auth(u),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["high_pct"] == 90 and body["mode"] == "LIVE"
        live = [d for d in body["devices"] if d["adapter"] != "simulator"]
        assert live[0]["dev_eui"] == "A84041000181C9F1"
        assert live[0]["adapter"] == "manual" and live[0]["scale"] == 0.01

        r = fast.patch(
            f"/water-sources/{src['id']}", json={"width_m": None}, headers=_auth(u)
        )
        assert r.status_code == 400
        r = fast.patch(
            f"/water-sources/{src['id']}", json={"name": None}, headers=_auth(u)
        )
        assert r.status_code == 400

    def test_delete_removes_everything(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u)
        fast.post(
            f"/water-sources/{src['id']}/simulate",
            json={"level_m": 0.2},
            headers=_auth(u),
        )
        r = fast.delete(f"/water-sources/{src['id']}", headers=_auth(u))
        assert r.status_code == 204
        with connection.cursor() as cursor:
            for table in (
                "water_source",
                "water_level_device",
                "water_level_reading",
                "water_level_alert",
            ):
                cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
                assert cursor.fetchone()[0] == 0, table


class TestSimulatorAndStatus:
    def test_brief_example(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u)
        r = fast.post(
            f"/water-sources/{src['id']}/simulate",
            json={"level_m": 3.62},
            headers=_auth(u),
        )
        assert r.status_code == 201, r.text
        s = r.json()
        assert s["data_status"] == "SIMULATED"
        m = s["metrics"]
        assert m["volume_m3"] == pytest.approx(86.88)
        assert m["free_capacity_m3"] == pytest.approx(28.32)
        assert m["available_m3"] == pytest.approx(76.88)
        assert round(m["fill_percent"], 1) == 75.4
        assert m["level_state"] == "NORMAL"
        assert s["open_alerts"] == []

        fast.post(
            f"/water-sources/{src['id']}/simulate",
            json={"level_m": 3.0},
            headers=_auth(u),
        )
        s = fast.get(f"/water-sources/{src['id']}/status", headers=_auth(u)).json()
        assert s["trend_24h_level_m"] == pytest.approx(-0.62)
        assert s["trend_24h_m3"] == pytest.approx(-14.88)

    def test_simulator_refused_when_live(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u, mode="LIVE")
        r = fast.post(
            f"/water-sources/{src['id']}/simulate",
            json={"level_m": 1},
            headers=_auth(u),
        )
        assert r.status_code == 409

    def test_out_of_range_is_invalid_and_raises_no_level_alert(
        self, fast, django_user_model
    ):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u)
        s = fast.post(
            f"/water-sources/{src['id']}/simulate",
            json={"level_m": 9},
            headers=_auth(u),
        ).json()
        assert s["data_status"] == "INVALID"
        assert s["last_reading"]["quality"] == "INVALID"
        assert s["open_alerts"] == []

    def test_history_ranges(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u)
        for level in (1.0, 2.0):
            fast.post(
                f"/water-sources/{src['id']}/simulate",
                json={"level_m": level},
                headers=_auth(u),
            )
        _backdate_readings(src["id"], 60 * 48)  # both two days old
        fast.post(
            f"/water-sources/{src['id']}/simulate",
            json={"level_m": 3.0},
            headers=_auth(u),
        )
        day = fast.get(
            f"/water-sources/{src['id']}/history?range=24h", headers=_auth(u)
        ).json()
        week = fast.get(
            f"/water-sources/{src['id']}/history?range=7d", headers=_auth(u)
        ).json()
        assert [p["level_m"] for p in day["points"]] == [3.0]
        assert [p["level_m"] for p in week["points"]] == [1.0, 2.0, 3.0]
        assert week["points"][-1]["volume_m3"] == pytest.approx(72.0)
        r = fast.get(f"/water-sources/{src['id']}/history?range=1y", headers=_auth(u))
        assert r.status_code == 422

    def test_history_is_downsampled(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u)
        now = svc.utcnow()
        with connection.cursor() as cursor:
            for i in range(svc.HISTORY_MAX_POINTS * 3):
                cursor.execute(
                    "INSERT INTO water_level_reading (water_source_id, device_id, "
                    "mode, measured_at, received_at, level_m, quality) "
                    "VALUES (%s, 'sim', 'SIMULATED', %s, %s, 2.0, 'GOOD')",
                    [src["id"], now - datetime.timedelta(minutes=i * 4), now],
                )
        pts = fast.get(
            f"/water-sources/{src['id']}/history?range=7d", headers=_auth(u)
        ).json()["points"]
        assert 0 < len(pts) <= svc.HISTORY_MAX_POINTS
        assert all(p["level_m"] == pytest.approx(2.0) for p in pts)


class TestReadings:
    def test_standard_payload_via_simulator_device(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u)
        r = fast.post(
            "/water-level/readings",
            json={
                "water_source_id": src["id"],
                "device_id": f"simulator-ws{src['id']}",
                "mode": "SIMULATED",
                "measured_at": (
                    svc.utcnow() - datetime.timedelta(minutes=2)
                ).isoformat(),
                "level_m": 3.62,
                "quality": "GOOD",
                "battery_percent": None,
                "rssi": None,
                "raw_payload": None,
            },
            headers=_auth(u),
        )
        assert r.status_code == 201, r.text
        assert r.json()["mode"] == "SIMULATED" and r.json()["level_m"] == 3.62

    def test_live_device_applies_scale(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(
            fast,
            u,
            mode="LIVE",
            live_device={"device_id": "rs485-lb-1", "scale": 0.01, "offset_m": 0.1},
        )
        r = fast.post(
            "/water-level/readings",
            json={"device_id": "rs485-lb-1", "raw_level": 250, "rssi": -97},
            headers=_auth(u),
        )
        assert r.status_code == 201, r.text
        assert r.json()["level_m"] == pytest.approx(2.6)
        s = fast.get(f"/water-sources/{src['id']}/status", headers=_auth(u)).json()
        assert s["data_status"] == "LIVE"
        assert s["last_reading"]["rssi"] == -97

        # Source picks its live device by mode when no device_id is sent.
        r = fast.post(
            "/water-level/readings",
            json={"water_source_id": src["id"], "raw_level": 300},
            headers=_auth(u),
        )
        assert r.json()["device_id"] == "rs485-lb-1"
        # The simulator device is refused while LIVE.
        r = fast.post(
            "/water-level/readings",
            json={"device_id": f"simulator-ws{src['id']}", "level_m": 1},
            headers=_auth(u),
        )
        assert r.status_code == 409

    def test_bad_payloads(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        src = _create(fast, u)
        for body, code in (
            ({"water_source_id": src["id"]}, 400),
            ({"level_m": 1}, 400),
            ({"water_source_id": src["id"], "raw_level": 1}, 400),
            (
                {
                    "water_source_id": src["id"],
                    "level_m": 1,
                    "measured_at": (
                        svc.utcnow() + datetime.timedelta(hours=1)
                    ).isoformat(),
                },
                400,
            ),
        ):
            r = fast.post("/water-level/readings", json=body, headers=_auth(u))
            assert r.status_code == code, (body, r.text)


class TestAlerts:
    def _sim(self, fast, u, sid, level):
        return fast.post(
            f"/water-sources/{sid}/simulate", json={"level_m": level}, headers=_auth(u)
        ).json()

    def test_open_once_escalate_and_resolve(self, fast, django_user_model, enqueued):
        u = _user(django_user_model, "wl-a")
        sid = _create(fast, u)["id"]

        s = self._sim(fast, u, sid, 1.2)  # 25 % → LOW
        assert [a["kind"] for a in s["open_alerts"]] == ["LOW"]
        self._sim(fast, u, sid, 1.1)  # still LOW: no second alert/email
        s = self._sim(fast, u, sid, 0.5)  # ~10 % → CRITICAL replaces LOW
        assert [a["kind"] for a in s["open_alerts"]] == ["CRITICAL"]
        s = self._sim(fast, u, sid, 4.7)  # ~98 % → HIGH
        assert [a["kind"] for a in s["open_alerts"]] == ["HIGH"]
        s = self._sim(fast, u, sid, 3.0)
        assert s["open_alerts"] == []

        history = fast.get(f"/water-sources/{sid}/alerts", headers=_auth(u)).json()
        assert [a["kind"] for a in history] == ["HIGH", "CRITICAL", "LOW"]
        assert all(a["resolved_at"] for a in history)
        emails = [
            kw["alert_id"] for name, kw in enqueued if name == svc.SEND_ALERT_EMAIL_TASK
        ]
        assert sorted(emails) == sorted(a["id"] for a in history)

    def test_threshold_change_reevaluates(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        sid = _create(fast, u)["id"]
        self._sim(fast, u, sid, 2.4)  # 50 %
        fast.patch(
            f"/water-sources/{sid}", json={"low_warning_pct": 60}, headers=_auth(u)
        )
        s = fast.get(f"/water-sources/{sid}/status", headers=_auth(u)).json()
        assert [a["kind"] for a in s["open_alerts"]] == ["LOW"]

    def test_offline_sweep_opens_and_recovers(self, fast, django_user_model):
        u = _user(django_user_model, "wl-a")
        sid = _create(fast, u)["id"]
        self._sim(fast, u, sid, 1.2)  # LOW
        _backdate_readings(sid, 121)
        assert tasks_water.check_water_level_offline() == {"opened": 1}
        s = fast.get(f"/water-sources/{sid}/status", headers=_auth(u)).json()
        assert s["data_status"] == "OFFLINE"
        # Offline keeps the last level alert: the level is unknown, not fixed.
        assert sorted(a["kind"] for a in s["open_alerts"]) == ["LOW", "OFFLINE"]
        assert tasks_water.check_water_level_offline() == {"opened": 0}

        s = self._sim(fast, u, sid, 3.0)
        assert s["open_alerts"] == []

    def test_alert_email(self, fast, django_user_model, monkeypatch):
        sent: list[dict] = []
        monkeypatch.setattr(
            tasks_water, "_send_email", lambda **kw: sent.append(kw) or True
        )
        u = _user(django_user_model, "wl-a")
        sid = _create(fast, u)["id"]
        alert_id = self._sim(fast, u, sid, 0.5)["open_alerts"][0]["id"]
        assert tasks_water.send_water_level_alert_email(alert_id=alert_id) == {
            "sent": 1,
            "alert_id": alert_id,
        }
        assert sent[0]["to"] == "wl-a@example.com"
        assert "Main Basin" in sent[0]["subject"]
        assert "niveau critique" in sent[0]["subject"]

        self._sim(fast, u, sid, 3.0)  # resolved before the worker ran
        assert tasks_water.send_water_level_alert_email(alert_id=alert_id) == {
            "sent": 0,
            "reason": "alert_resolved",
        }
        assert tasks_water.send_water_level_alert_email(alert_id=999999)["sent"] == 0
