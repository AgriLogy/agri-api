"""fastapp MQTT ingest — transport routing + parsing (no broker, no DB).

The dual-ORM row/alert parity of the SHARED handlers (``handle_metrics`` /
``handle_chirpstack_uplink``) is already proven over the HTTP surface by
``test_ingest_parity``. These tests cover the NEW transport layer only: each
topic filter parses its payload and calls the RIGHT ``fastapp.ingest`` handler
with the ``client`` / ``sensor_key`` / ``metrics`` derived from the topic + body,
and a malformed message is swallowed (never crashes the subscriber loop).

Handlers + ``session_scope`` are monkeypatched to no-ops, so nothing here touches
Postgres, Redis, or a broker — pure, millisecond-fast unit tests.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os

import pytest

from fastapp import ingest, mqtt

_UTC = datetime.timezone.utc


class _Msg:
    """Minimal stand-in for a paho MQTTMessage (topic + raw payload bytes)."""

    def __init__(self, topic: str, payload) -> None:
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else payload.encode()


@pytest.fixture
def calls(monkeypatch):
    """Record handler invocations; stub session_scope + the two shared handlers
    so the transport is exercised in isolation (no DB)."""
    rec: dict[str, list] = {"metrics": [], "chirp": []}

    @contextlib.contextmanager
    def fake_scope(*a, **k):
        yield object()

    monkeypatch.setattr(mqtt, "session_scope", fake_scope)

    def fake_metrics(session, *, client, metrics, timestamp=None):
        rec["metrics"].append(
            {"client": client, "metrics": metrics, "timestamp": timestamp}
        )
        return len(metrics)

    def fake_chirp(session, **kw):
        rec["chirp"].append(kw)
        return 3

    monkeypatch.setattr(ingest, "handle_metrics", fake_metrics)
    monkeypatch.setattr(ingest, "handle_chirpstack_uplink", fake_chirp)
    return rec


@pytest.fixture
def mi():
    return mqtt.MqttIngest()


# ---------------------------------------------------------------------------
# Generic weather topic: {prefix}/{client}/weather
# ---------------------------------------------------------------------------
def test_weather_routes_filtered_metrics(mi, calls):
    body = json.dumps(
        {"temperature_weather": 21.5, "humidity_weather": 63.0, "bogus": 9}
    )
    mi._on_weather(None, None, _Msg("agrilogy/user1/weather", body))
    assert calls["metrics"] == [
        {
            "client": "user1",
            "metrics": {"temperature_weather": 21.5, "humidity_weather": 63.0},
            "timestamp": None,
        }
    ]


def test_weather_no_known_metrics_is_noop(mi, calls):
    mi._on_weather(None, None, _Msg("agrilogy/user1/weather", "{}"))
    assert calls["metrics"] == []


# ---------------------------------------------------------------------------
# Single-sensor topic: {prefix}/{client}/sensor/{sensor_key}
# ---------------------------------------------------------------------------
def test_sensor_routes_single_metric_with_timestamp(mi, calls):
    body = json.dumps({"value": 8.5, "timestamp": "2026-06-01T09:00:00+00:00"})
    mi._on_sensor(None, None, _Msg("agrilogy/user1/sensor/soil_ph", body))
    assert calls["metrics"] == [
        {
            "client": "user1",
            "metrics": {"soil_ph": 8.5},
            "timestamp": datetime.datetime(2026, 6, 1, 9, 0, tzinfo=_UTC),
        }
    ]


def test_sensor_unknown_key_dropped(mi, calls):
    mi._on_sensor(None, None, _Msg("agrilogy/user1/sensor/bogus_key", '{"value":1}'))
    assert calls["metrics"] == []


def test_sensor_missing_value_dropped(mi, calls):
    mi._on_sensor(None, None, _Msg("agrilogy/user1/sensor/soil_ph", '{"nope":1}'))
    assert calls["metrics"] == []


# ---------------------------------------------------------------------------
# Bivocom (bridge-shaped) topic: {prefix}/{client}/bivocom
# ---------------------------------------------------------------------------
def test_bivocom_routes_tags_as_metrics(mi, calls):
    body = json.dumps(
        {
            "device_id": "router-user-user1",
            "timestamp": "2026-06-01T09:00:00Z",
            "tags": {"temperature_weather": 22.5, "ta": 1},  # 'ta' unknown → filtered
        }
    )
    mi._on_bivocom(None, None, _Msg("agrilogy/user1/bivocom", body))
    assert calls["metrics"] == [
        {
            "client": "user1",
            "metrics": {"temperature_weather": 22.5},
            "timestamp": datetime.datetime(2026, 6, 1, 9, 0, tzinfo=_UTC),
        }
    ]


# ---------------------------------------------------------------------------
# ChirpStack topic: application/+/device/+/event/up
# ---------------------------------------------------------------------------
def test_chirpstack_routes_uplink_fields(mi, calls):
    body = json.dumps(
        {
            "deviceInfo": {"devEui": "a1b2c3d4e5f60718", "deviceName": "probe-1"},
            "rxInfo": [{"rssi": -80.0, "snr": 9.2}],
            "txInfo": {"frequency": 868100000},
            "fPort": 2,
            "fCnt": 11,
            "object": {"pH": 7.5, "BatV": 3.6},
        }
    )
    mi._on_chirpstack(
        None, None, _Msg("application/1/device/a1b2c3d4e5f60718/event/up", body)
    )
    assert len(calls["chirp"]) == 1
    kw = calls["chirp"][0]
    assert kw["dev_eui"] == "a1b2c3d4e5f60718"
    assert kw["device_name"] == "probe-1"
    assert (kw["rssi"], kw["snr"], kw["frequency"]) == (-80.0, 9.2, 868100000)
    assert kw["f_port"] == 2 and kw["f_cnt"] == 11
    assert kw["obj"] == {"pH": 7.5, "BatV": 3.6}


# ---------------------------------------------------------------------------
# Robustness: a bad message must never escape the callback.
# ---------------------------------------------------------------------------
def test_malformed_payloads_are_swallowed(mi, calls):
    mi._on_weather(None, None, _Msg("agrilogy/user1/weather", b"not-json"))
    mi._on_chirpstack(None, None, _Msg("application/1/device/x/event/up", b"not-json"))
    mi._on_bivocom(None, None, _Msg("agrilogy/user1/bivocom", "[]"))  # not an object
    assert calls["metrics"] == [] and calls["chirp"] == []


def test_subscriptions_cover_all_four_sources(mi):
    assert [t for t, _ in mi._subscriptions()] == [
        "application/+/device/+/event/up",
        "agrilogy/+/weather",
        "agrilogy/+/sensor/+",
        "agrilogy/+/bivocom",
    ]


def test_filters_match_concrete_topics_uniquely(mi):
    """Broker-side wiring proof (no broker needed): every concrete topic paho
    will deliver matches EXACTLY its one intended subscription filter — so the
    per-filter callbacks route correctly and no two filters overlap. Guards
    against a filter typo or an accidental wildcard collision."""
    from paho.mqtt.matcher import MQTTMatcher

    def matches(sub: str, topic: str) -> bool:
        m = MQTTMatcher()
        m[sub] = True
        try:
            next(m.iter_match(topic))
            return True
        except StopIteration:
            return False

    subs = [t for t, _ in mi._subscriptions()]
    expected = {
        "application/1/device/a1b2c3d4e5f60718/event/up": "application/+/device/+/event/up",
        "agrilogy/user1/weather": "agrilogy/+/weather",
        "agrilogy/user1/sensor/soil_ph": "agrilogy/+/sensor/+",
        "agrilogy/user1/bivocom": "agrilogy/+/bivocom",
    }
    for topic, want in expected.items():
        assert [s for s in subs if matches(s, topic)] == [want], topic


# ---------------------------------------------------------------------------
# parse_timestamp helper (shared by the MQTT single-sensor / bivocom paths).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("garbage", None),
        ("2026-06-01T09:00:00Z", datetime.datetime(2026, 6, 1, 9, 0, tzinfo=_UTC)),
        (
            "2026-06-01T09:00:00+00:00",
            datetime.datetime(2026, 6, 1, 9, 0, tzinfo=_UTC),
        ),
    ],
)
def test_parse_timestamp(value, expected):
    assert ingest.parse_timestamp(value) == expected


# ---------------------------------------------------------------------------
# Liveness file — what the container healthcheck stats. It must exist ONLY while
# the subscriber holds a broker connection (a never-connected subscriber used to
# report `Up (healthy)` while LoRa ingest was dead).
# ---------------------------------------------------------------------------
@pytest.fixture
def health_mi(tmp_path, monkeypatch):
    monkeypatch.setenv("MQTT_HEALTH_FILE", str(tmp_path / "mqtt-healthy"))

    from fastapp.settings import get_settings

    get_settings.cache_clear()
    try:
        yield mqtt.MqttIngest()
    finally:
        get_settings.cache_clear()


def test_health_file_tracks_connection_state(health_mi):
    path = health_mi.settings.mqtt_health_file
    assert not os.path.exists(path)  # nothing claimed before CONNACK

    health_mi._on_connect(_FakeClient(), None, None, 0)
    assert os.path.exists(path)

    health_mi._on_disconnect(_FakeClient(), None, None, 1)
    assert not os.path.exists(path)


def test_health_file_absent_on_failed_connect(health_mi):
    health_mi._on_connect(_FakeClient(), None, None, 5)  # not authorised
    assert not os.path.exists(health_mi.settings.mqtt_health_file)


class _FakeClient:
    """Just enough paho client for the connect callbacks (subscribe only)."""

    def __init__(self) -> None:
        self.subscribed = []

    def subscribe(self, subs):
        self.subscribed.append(subs)
