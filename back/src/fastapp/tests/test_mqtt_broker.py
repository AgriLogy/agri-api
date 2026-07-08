"""MQTT ingest — REAL broker wiring (no DB).

Unlike ``test_mqtt_ingest`` (which mocks paho entirely), these drive an actual
``mosquitto`` broker: a real paho publish crosses a socket, the subscriber's
network thread receives it, and the right ``fastapp.ingest`` handler is called
with the payload parsed from topic + body. The handlers + ``session_scope`` are
stubbed to record calls, so this needs NO database — it runs anywhere mosquitto
is installed (locally + the e2e CI job), proving the transport plumbing that the
unit tests can't.

The full row/alert-through-Postgres round-trip is in ``test_mqtt_e2e``.
"""

from __future__ import annotations

import contextlib

import pytest

from fastapp import ingest
from fastapp import mqtt as mqtt_mod
from fastapp.tests.conftest import wait_until


@pytest.fixture
def recorded(monkeypatch):
    """Stub the shared handlers + session_scope; capture invocations (the
    subscriber calls these on its network thread — plain list append is fine)."""
    rec: dict[str, list] = {"metrics": [], "chirp": []}

    @contextlib.contextmanager
    def fake_scope(*a, **k):
        yield object()

    monkeypatch.setattr(mqtt_mod, "session_scope", fake_scope)
    monkeypatch.setattr(
        ingest,
        "handle_metrics",
        lambda session, *, client, metrics, timestamp=None: (
            rec["metrics"].append(
                {"client": client, "metrics": metrics, "timestamp": timestamp}
            )
            or len(metrics)
        ),
    )
    monkeypatch.setattr(
        ingest,
        "handle_chirpstack_uplink",
        lambda session, **kw: rec["chirp"].append(kw) or 3,
    )
    return rec


def test_weather_message_reaches_handler(mqtt_subscriber, mqtt_publish, recorded):
    mqtt_publish(
        "agrilogy/user1/weather",
        {"temperature_weather": 21.5, "humidity_weather": 63.0, "bogus": 9},
    )
    assert wait_until(lambda: recorded["metrics"]), "handler never called"
    assert recorded["metrics"] == [
        {
            "client": "user1",
            "metrics": {"temperature_weather": 21.5, "humidity_weather": 63.0},
            "timestamp": None,
        }
    ]


def test_sensor_message_reaches_handler(mqtt_subscriber, mqtt_publish, recorded):
    mqtt_publish("agrilogy/user1/sensor/soil_ph", {"value": 8.5})
    assert wait_until(lambda: recorded["metrics"])
    assert recorded["metrics"][0]["client"] == "user1"
    assert recorded["metrics"][0]["metrics"] == {"soil_ph": 8.5}


def test_bivocom_message_reaches_handler(mqtt_subscriber, mqtt_publish, recorded):
    mqtt_publish(
        "agrilogy/user1/bivocom",
        {"device_id": "router-user-user1", "tags": {"temperature_weather": 22.5}},
    )
    assert wait_until(lambda: recorded["metrics"])
    assert recorded["metrics"][0]["metrics"] == {"temperature_weather": 22.5}


def test_chirpstack_message_reaches_handler(mqtt_subscriber, mqtt_publish, recorded):
    mqtt_publish(
        "application/1/device/a1b2c3d4e5f60718/event/up",
        {
            "deviceInfo": {"devEui": "a1b2c3d4e5f60718", "deviceName": "probe-1"},
            "rxInfo": [{"rssi": -80.0, "snr": 9.2}],
            "txInfo": {"frequency": 868100000},
            "fPort": 2,
            "fCnt": 11,
            "object": {"pH": 7.5, "BatV": 3.6},
        },
    )
    assert wait_until(lambda: recorded["chirp"])
    kw = recorded["chirp"][0]
    assert kw["dev_eui"] == "a1b2c3d4e5f60718"
    assert (kw["rssi"], kw["snr"], kw["frequency"]) == (-80.0, 9.2, 868100000)
    assert kw["obj"] == {"pH": 7.5, "BatV": 3.6}


def test_malformed_payload_dropped_and_subscriber_survives(
    mqtt_subscriber, mqtt_publish, recorded
):
    # A bad message must be swallowed by the callback...
    mqtt_publish("agrilogy/user1/weather", b"not-json")
    # ...and the subscriber must keep processing the next (valid) one.
    mqtt_publish("agrilogy/user1/weather", {"temperature_weather": 30.0})
    assert wait_until(lambda: recorded["metrics"]), "subscriber died on bad message"
    assert recorded["metrics"] == [
        {"client": "user1", "metrics": {"temperature_weather": 30.0}, "timestamp": None}
    ]


def test_unknown_sensor_key_not_dispatched(mqtt_subscriber, mqtt_publish, recorded):
    mqtt_publish("agrilogy/user1/sensor/bogus_key", {"value": 1})
    # Give the message time to arrive, then assert it was NOT dispatched.
    mqtt_publish("agrilogy/user1/weather", {"temperature_weather": 12.0})
    assert wait_until(lambda: recorded["metrics"])  # the weather one lands
    assert all(m["metrics"] != {"bogus_key": 1} for m in recorded["metrics"])
