"""Shared fixtures for the MQTT ingest broker + e2e tests.

These tests drive a REAL paho publish → subscriber → dispatch/DB round-trip
against a real MQTT broker — the piece the mocked ``test_mqtt_ingest`` unit
tests deliberately stub out. The broker is a throwaway ``mosquitto`` process
the fixture launches on an ephemeral loopback port (anonymous, no persistence);
if the ``mosquitto`` binary isn't installed the broker fixture SKIPS, exactly
like the Postgres-gated parity tests skip without a database.

Local: ``brew install mosquitto`` (macOS) / ``apt-get install -y mosquitto``.
CI: the dedicated e2e job installs mosquitto before running these.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid

import pytest

# mosquitto may live outside the default PATH (Homebrew sbin, /usr/sbin).
_MOSQUITTO_CANDIDATES = (
    "mosquitto",
    "/opt/homebrew/sbin/mosquitto",
    "/usr/local/sbin/mosquitto",
    "/usr/sbin/mosquitto",
)


def _find_mosquitto() -> str | None:
    for cand in _MOSQUITTO_CANDIDATES:
        found = (
            shutil.which(cand)
            if "/" not in cand
            else (cand if _is_exec(cand) else None)
        )
        if found:
            return found
    return None


def _is_exec(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(host: str, port: int, *, timeout: float, proc=None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            if proc is not None and proc.poll() is not None:
                return False
            time.sleep(0.1)
    return False


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout`` elapses.

    Message delivery is asynchronous (publish → broker → subscriber network
    thread → handler), so assertions poll instead of sleeping a fixed amount.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture(scope="module")
def mqtt_broker(tmp_path_factory):
    """A throwaway mosquitto broker on an ephemeral loopback port.

    Module-scoped so one broker serves a whole test file; tests run
    sequentially and each uses a uniquely-named subscriber, so there is no
    cross-test bleed. Skips when mosquitto isn't installed.
    """
    binary = _find_mosquitto()
    if binary is None:
        # In CI the dedicated mqtt-e2e job sets MQTT_REQUIRE_BROKER so a missing
        # broker FAILS the gate instead of silently skipping (a skipped e2e is
        # indistinguishable from a passing one — it must never quietly no-op).
        if os.environ.get("MQTT_REQUIRE_BROKER"):
            pytest.fail(
                "MQTT_REQUIRE_BROKER is set but no mosquitto binary was found — "
                "the e2e gate cannot run. Install mosquitto in this environment."
            )
        pytest.skip("mosquitto binary not found (brew/apt install mosquitto)")

    port = _free_port()
    conf = tmp_path_factory.mktemp("mosquitto") / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
    )
    proc = subprocess.Popen(
        [binary, "-c", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_port("127.0.0.1", port, timeout=10, proc=proc):
        proc.terminate()
        pytest.fail("mosquitto failed to start")

    yield ("127.0.0.1", port)

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def mqtt_subscriber(mqtt_broker, monkeypatch):
    """A live ``MqttIngest`` connected to the broker with all filters subscribed.

    Yields the ``MqttIngest`` instance once its SUBACK has landed, so a
    subsequent publish is guaranteed to be routed (no lost-message race).
    """
    host, port = mqtt_broker
    monkeypatch.setenv("MQTT_HOST", host)
    monkeypatch.setenv("MQTT_PORT", str(port))
    monkeypatch.setenv("MQTT_CLIENT_ID", f"test-ingest-{uuid.uuid4().hex[:8]}")

    from fastapp import mqtt as mqtt_mod
    from fastapp.settings import get_settings

    get_settings.cache_clear()
    mi = mqtt_mod.MqttIngest()
    mi._configure()

    subscribed = threading.Event()
    mi.client.on_subscribe = lambda *a, **k: subscribed.set()

    mi.client.connect(host, port, 30)
    mi.client.loop_start()
    if not subscribed.wait(10):
        mi.client.loop_stop()
        mi.client.disconnect()
        pytest.fail("subscriber did not receive SUBACK")

    yield mi

    mi.client.loop_stop()
    mi.client.disconnect()
    get_settings.cache_clear()


@pytest.fixture
def mqtt_publish(mqtt_broker):
    """Return ``publish(topic, payload)`` — dict/list payloads are JSON-encoded;
    str/bytes pass through. Waits for the publish to be acked before returning.
    """
    import paho.mqtt.client as mqtt

    host, port = mqtt_broker
    pub = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"test-pub-{uuid.uuid4().hex[:8]}",
    )
    pub.connect(host, port, 30)
    pub.loop_start()

    def _publish(topic: str, payload, qos: int = 1) -> None:
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload)
        info = pub.publish(topic, payload, qos=qos)
        info.wait_for_publish(5)

    yield _publish

    pub.loop_stop()
    pub.disconnect()
