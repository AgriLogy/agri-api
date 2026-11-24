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
    yield {"redis": redis, "sent": sent, "calls": calls}
    get_settings.cache_clear()


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
