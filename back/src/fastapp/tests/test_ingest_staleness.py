"""#429: ``check_ingest_staleness`` emails ops when an ingest feed goes quiet,
once per renotify window, and once more when data resumes.

The pure staleness rule is tested without a database. The task itself runs
against Postgres (``to_regclass`` + real ``lora_uplink`` /
``analytics_temperatureweather`` rows); email and the Redis slot are faked.
"""

from __future__ import annotations

import datetime

import pytest
from django.conf import settings as dj_settings

from fastapp import tasks_ops
from fastapp.settings import get_settings

NOW = datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.timezone.utc)


def _h(hours: float) -> datetime.datetime:
    return NOW - datetime.timedelta(hours=hours)


# --- pure rule ---------------------------------------------------------------


def test_find_stale_feeds_flags_only_feeds_past_threshold():
    stale = tasks_ops.find_stale_feeds(
        {"lora": _h(5), "bivocom": _h(1)}, now=NOW, threshold_hours=2
    )
    assert stale == {"lora": 5.0}


def test_find_stale_feeds_skips_feeds_without_data():
    assert tasks_ops.find_stale_feeds({"lora": None}, now=NOW, threshold_hours=2) == {}


def test_find_stale_feeds_treats_naive_timestamps_as_utc():
    naive = _h(3).replace(tzinfo=None)
    assert tasks_ops.find_stale_feeds({"lora": naive}, now=NOW, threshold_hours=2) == {
        "lora": 3.0
    }


def test_ops_recipients_fall_back_to_internal_list(monkeypatch):
    monkeypatch.setenv("OPS_ALERT_EMAILS", "")
    monkeypatch.setenv("INTERNAL_FEEDBACK_EMAILS", "a@x.com, b@x.com")
    get_settings.cache_clear()
    try:
        assert get_settings().ops_alert_emails_list == ["a@x.com", "b@x.com"]
        monkeypatch.setenv("OPS_ALERT_EMAILS", "ops@x.com")
        get_settings.cache_clear()
        assert get_settings().ops_alert_emails_list == ["ops@x.com"]
    finally:
        get_settings.cache_clear()


# --- task against Postgres -----------------------------------------------------

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="reads lora_uplink / analytics_temperatureweather via SQLAlchemy",
)


@pytest.fixture
def ops(monkeypatch):
    """Fake Redis slots + captured email, fixed clock, one ops recipient."""
    slots: set[str] = set()
    sent: list[tuple[str, str]] = []

    def _claim(key, ttl):
        if key in slots:
            return False
        slots.add(key)
        return True

    def _release(key):
        if key in slots:
            slots.discard(key)
            return True
        return False

    monkeypatch.setattr(tasks_ops, "claim_alert_slot", _claim)
    monkeypatch.setattr(tasks_ops, "release_alert_slot", _release)
    monkeypatch.setattr(
        tasks_ops,
        "_send_email",
        lambda *, to, subject, body: sent.append((to, subject)) or True,
    )
    monkeypatch.setattr(tasks_ops, "_utcnow", lambda: NOW)
    monkeypatch.setenv("OPS_ALERT_EMAILS", "ops@x.com")
    get_settings.cache_clear()
    yield {"slots": slots, "sent": sent}
    get_settings.cache_clear()


@pytest.fixture
def _clean_uplinks(db):
    from apps.lorawan.chirpstack.models import LoraUplink

    LoraUplink.objects.all().delete()  # unmanaged — not truncated between tests
    yield
    LoraUplink.objects.all().delete()


def _uplink(ts):
    from apps.lorawan.chirpstack.models import LoraUplink

    LoraUplink.objects.create(dev_eui="e1", device_name="e1", received_at=ts)


def _weather(django_user_model, ts):
    from analytics.models import TemperatureWeather
    from apps.irrigation.models import Zone

    u = django_user_model.objects.create_user(
        username="w", email="w@x.com", password="x", firstname="W"
    )
    z = Zone.objects.create(
        user=u,
        name="Z",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
        elevation_m=120.0,
    )
    TemperatureWeather.objects.create(user=u, zone=z, value=20.0, timestamp=ts)


@_requires_pg
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_clean_uplinks")
class TestCheckIngestStaleness:
    def test_alerts_once_per_window_for_a_quiet_feed(self, ops, django_user_model):
        _uplink(_h(9))
        _weather(django_user_model, _h(0.5))

        first = tasks_ops.check_ingest_staleness()
        assert first["stale"] == {"lora": 9.0}
        assert first["alerted"] == ["lora"]
        assert ops["sent"] == [
            ("ops@x.com", "[Agrogo ops] No LoRaWAN uplinks (ChirpStack) for 9 h")
        ]

        # Next beat inside the renotify window: no second email.
        second = tasks_ops.check_ingest_staleness()
        assert second["alerted"] == []
        assert len(ops["sent"]) == 1

    def test_recovery_sends_one_resumed_email_and_rearms(self, ops):
        _uplink(_h(9))
        tasks_ops.check_ingest_staleness()
        assert "lora" in ops["slots"]

        _uplink(_h(0.1))  # data resumes
        result = tasks_ops.check_ingest_staleness()
        assert result["recovered"] == ["lora"]
        assert (
            ops["sent"][-1][1]
            == "[Agrogo ops] LoRaWAN uplinks (ChirpStack) data resumed"
        )
        assert "lora" not in ops["slots"]

        # Steady state after recovery: nothing more.
        n = len(ops["sent"])
        assert tasks_ops.check_ingest_staleness()["recovered"] == []
        assert len(ops["sent"]) == n

    def test_empty_feeds_never_alert(self, ops):
        result = tasks_ops.check_ingest_staleness()
        assert result["stale"] == {}
        assert ops["sent"] == []
