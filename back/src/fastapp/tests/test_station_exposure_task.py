"""``check_station_exposure``: flag weather stations that look sheltered.

Runs against Postgres (device + hourly readings through the Django models).
The outdoor reference (Open-Meteo), Redis and email are faked. The sheltered
case replays Router02's real day (2026-11-23/24): dark, closed space.
"""

from __future__ import annotations

import datetime
import json

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp import tasks_ops
from fastapp.settings import get_settings

NOW = datetime.datetime(2026, 11, 24, 14, 0, tzinfo=datetime.timezone.utc)

# (station °C, outdoor °C, outdoor wind 10 m m/s, outdoor shortwave W/m²) for
# the 24 hours ending at NOW, oldest first: Router02's real day.
ROUTER02 = [
    (26.1, 27.1, 3.64, 568.0), (25.2, 26.8, 3.68, 456.0), (24.7, 26.3, 3.16, 302.0),
    (24.5, 25.1, 2.1, 120.0), (24.1, 22.3, 1.17, 5.0), (23.7, 20.6, 0.95, 0.0),
    (23.5, 20.3, 0.85, 0.0), (23.3, 20.3, 0.81, 0.0), (23.2, 20.1, 0.94, 0.0),
    (23.1, 19.7, 1.12, 0.0), (22.9, 19.0, 1.1, 0.0), (22.7, 18.6, 1.2, 0.0),
    (22.6, 17.3, 1.46, 0.0), (22.6, 15.8, 1.61, 0.0), (22.5, 15.4, 1.66, 0.0),
    (22.4, 15.5, 1.88, 0.0), (22.2, 15.2, 2.06, 0.0), (22.1, 15.0, 2.19, 0.0),
    (22.1, 16.1, 2.25, 41.0), (22.7, 19.3, 2.16, 184.0), (23.8, 21.8, 2.91, 348.0),
    (25.1, 23.8, 3.47, 501.0), (25.8, 24.8, 3.28, 519.0), (25.7, 25.1, 3.85, 494.0),
]  # fmt: skip

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="reads analytics_* readings via SQLAlchemy",
)


def _hour(i: int) -> datetime.datetime:
    """Start of the i-th hour of the window (0 = oldest)."""
    return NOW - datetime.timedelta(hours=24 - i)


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, object] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.data if k.startswith(prefix)]

    def mget(self, keys):
        return [self.data.get(k) for k in keys]


@pytest.fixture
def env(monkeypatch):
    from apps.sensors import forecast_provider

    redis = _FakeRedis()
    sent: list[tuple[str, str, str]] = []
    reference = {
        _hour(i).strftime("%Y-%m-%dT%H"): {"temp_c": t, "wind_ms": w, "rs_wm2": rs}
        for i, (_, t, w, rs) in enumerate(ROUTER02)
    }
    calls: list[tuple] = []

    def fake_reference(*, start, end, latitude, longitude):
        calls.append((latitude, longitude))
        return reference

    monkeypatch.setattr(tasks_ops, "_redis", lambda: redis)
    monkeypatch.setattr(tasks_ops, "_utcnow", lambda: NOW)
    monkeypatch.setattr(
        tasks_ops,
        "_send_email",
        lambda *, to, subject, body: sent.append((to, subject, body)) or True,
    )
    monkeypatch.setattr(
        forecast_provider, "fetch_openmeteo_hourly_reference", fake_reference
    )
    monkeypatch.setenv("OPS_ALERT_EMAILS", "ops@x.com")
    get_settings.cache_clear()
    _clear_placements()
    yield {"redis": redis, "sent": sent, "calls": calls}
    _clear_placements()
    get_settings.cache_clear()


def _clear_placements():
    from agri.core.database import session_scope
    from sqlalchemy import text

    from fastapp.station_placement import SETTING_KEY

    with session_scope(commit=True) as s:
        s.execute(
            text("DELETE FROM analytics_systemsetting WHERE key = :k"),
            {"k": SETTING_KEY},
        )


def _declare(device_id: int, placement: str) -> None:
    from agri.core.database import session_scope

    from fastapp.station_placement import set_placement

    with session_scope(commit=True) as s:
        set_placement(s, device_id, placement, username="ops")


def _station(django_user_model, *, serial, sheltered, lat=32.93, lon=-6.90):
    from analytics.models import SolarRadiation, TemperatureWeather, WindSpeed
    from apps.irrigation.models import Device, Zone

    u = django_user_model.objects.create_user(
        username=serial, email=f"{serial}@x.com", password="x", firstname="S"
    )
    if lat is not None:
        u.latitude, u.longitude = lat, lon
        u.save(update_fields=["latitude", "longitude"])
    z = Zone.objects.create(
        user=u,
        name="Zone 1",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
        elevation_m=790.0,
    )
    Device.objects.filter(serial=serial).delete()  # analytics_device isn't flushed
    d = Device.objects.create(
        user=u, zone=z, device_type="bivocom", serial=serial, name=serial
    )
    for i, (st, t, w, rs) in enumerate(ROUTER02):
        ts = _hour(i) + datetime.timedelta(minutes=20)
        temp, wind, sun = (st, 0.0, 0.0) if sheltered else (t, 0.75 * w, rs)
        for model, value in (
            (TemperatureWeather, temp),
            (WindSpeed, wind),
            (SolarRadiation, sun),
        ):
            model.objects.create(
                user=u, zone=z, device_id=d.id, value=value, timestamp=ts
            )
    return d


@_requires_pg
@pytest.mark.django_db(transaction=True)
class TestCheckStationExposure:
    def test_sheltered_station_is_flagged_once_and_stored(self, env, django_user_model):
        dev = _station(django_user_model, serial="router02", sheltered=True)

        out = tasks_ops.check_station_exposure()
        assert out["stations"] == {"router02": "sheltered"}
        assert out["alerted"] == ["router02"]
        ((to, subject, body),) = env["sent"]
        assert to == "ops@x.com" and "router02 looks sheltered" in subject
        assert "solar sensor reads ~0" in body and "nights" in body
        assert env["calls"] == [(32.93, -6.9)]  # the account's farm location

        report = json.loads(env["redis"].data[f"station-exposure:report:{dev.id}"])
        assert report["status"] == "sheltered" and report["hours_compared"] == 24
        assert report["sensors"] == {
            "solar": "sheltered",
            "wind": "sheltered",
            "temperature": "sheltered",
        }
        assert report["detected"] == "indoor" and report["placement"] == "auto"
        assert report["mismatch"] is False  # nothing declared
        assert {s["code"] for s in report["signals"]} == {
            "no_sun",
            "no_wind",
            "damped_temperature",
            "warm_nights",
        }
        assert tasks_ops.stored_exposure_reports()[0]["device"] == "router02"

        # Next day, still sheltered: no second email inside the renotify window.
        out = tasks_ops.check_station_exposure()
        assert out["stations"] == {"router02": "sheltered"} and out["alerted"] == []
        assert len(env["sent"]) == 1

    def test_outdoor_station_is_ok_and_silent(self, env, django_user_model):
        _station(django_user_model, serial="field-1", sheltered=False)
        out = tasks_ops.check_station_exposure()
        assert out["stations"] == {"field-1": "ok"} and out["alerted"] == []
        assert env["sent"] == []

    def test_station_without_a_location_is_skipped(self, env, django_user_model):
        _station(django_user_model, serial="nowhere", sheltered=True, lat=None)
        out = tasks_ops.check_station_exposure()
        assert out["stations"] == {"nowhere": "no_location"}
        assert env["sent"] == [] and env["calls"] == []

    def test_declared_indoor_is_checked_but_never_emailed(self, env, django_user_model):
        dev = _station(django_user_model, serial="router02", sheltered=True)
        _declare(dev.id, "indoor")
        out = tasks_ops.check_station_exposure()
        assert out["stations"] == {"router02": "sheltered"} and out["alerted"] == []
        assert env["sent"] == []
        report = json.loads(env["redis"].data[f"station-exposure:report:{dev.id}"])
        assert report["placement"] == "indoor" and report["mismatch"] is False

    def test_declared_outdoor_but_sheltered_is_a_mismatch(self, env, django_user_model):
        dev = _station(django_user_model, serial="router02", sheltered=True)
        _declare(dev.id, "outdoor")
        out = tasks_ops.check_station_exposure()
        assert out["alerted"] == ["router02"]
        ((_, _, body),) = env["sent"]
        assert "declared outdoors" in body and "'indoor'" in body
        (row,) = tasks_ops.list_stations()
        assert row["placement"] == "outdoor" and row["report"]["mismatch"] is True

    def test_declared_indoor_but_reads_outdoor_is_a_mismatch(
        self, env, django_user_model
    ):
        dev = _station(django_user_model, serial="field-1", sheltered=False)
        _declare(dev.id, "indoor")
        report = tasks_ops.check_station_now(dev.id)
        assert report["status"] == "ok" and report["detected"] == "outdoor"
        assert report["mismatch"] is True and env["sent"] == []

    def test_check_now_without_recent_readings(self, env, django_user_model):
        from apps.irrigation.models import Device

        dev = _station(django_user_model, serial="router02", sheltered=True)
        Device.objects.filter(serial="quiet").delete()
        quiet = Device.objects.create(
            user=dev.user, zone=dev.zone, device_type="bivocom", serial="quiet"
        )
        assert tasks_ops.check_station_now(quiet.id)["status"] == "no_readings"
        assert tasks_ops.check_station_now(10**9) is None

    def test_list_shows_declaration_and_latest_report(self, env, django_user_model):
        dev = _station(django_user_model, serial="router02", sheltered=True)
        (row,) = tasks_ops.list_stations()
        assert row["device_id"] == dev.id and row["report"] is None
        assert row["placement"] == "auto" and row["username"] == "router02"
        tasks_ops.check_station_exposure()
        _declare(dev.id, "indoor")  # declared after the check: list re-derives
        (row,) = tasks_ops.list_stations()
        assert row["placement"] == "indoor" and row["placement_updated_by"] == "ops"
        assert row["report"]["status"] == "sheltered"
        assert row["report"]["placement"] == "indoor"
        assert row["report"]["mismatch"] is False


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)
    from fastapp.main import app

    return TestClient(app)


def _auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


@_requires_pg
@pytest.mark.django_db(transaction=True)
class TestStationRoutes:
    def test_staff_declares_checks_and_lists(self, env, fast, django_user_model):
        dev = _station(django_user_model, serial="router02", sheltered=True)
        staff = django_user_model.objects.create_user(
            username="ops-staff", email="ops@x.com", password="x", is_staff=True
        )
        h = _auth(staff)
        r = fast.put(
            f"/admin/monitoring/stations/{dev.id}/placement",
            json={"placement": "indoor"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["placement"] == "indoor"
        r = fast.post(f"/admin/monitoring/stations/{dev.id}/check", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "sheltered" and env["sent"] == []
        body = fast.get("/admin/monitoring/stations", headers=h).json()
        assert body["placements"] == ["auto", "outdoor", "indoor"]
        (row,) = body["stations"]
        assert row["placement"] == "indoor"
        assert row["report"]["sensors"]["solar"] == "sheltered"

    def test_validation_and_access(self, env, fast, django_user_model):
        dev = _station(django_user_model, serial="router02", sheltered=True)
        staff = django_user_model.objects.create_user(
            username="ops-staff", email="ops@x.com", password="x", is_staff=True
        )
        h = _auth(staff)
        url = f"/admin/monitoring/stations/{dev.id}/placement"
        assert fast.put(url, json={"placement": "roof"}, headers=h).status_code == 400
        missing = "/admin/monitoring/stations/999999999/placement"
        assert (
            fast.put(missing, json={"placement": "indoor"}, headers=h).status_code
            == 404
        )
        assert (
            fast.post(
                "/admin/monitoring/stations/999999999/check", headers=h
            ).status_code
            == 404
        )
        owner = _auth(dev.user)  # a farmer, not staff
        assert (
            fast.put(url, json={"placement": "indoor"}, headers=owner).status_code
            == 403
        )
        assert fast.get("/admin/monitoring/stations", headers=owner).status_code == 403
