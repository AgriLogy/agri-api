"""fastapp MQTT ingest — a persistent subscriber that consumes sensor data
over MQTT and writes it through the SAME handlers the HTTP webhooks use.

Run as its own process/container: ``docker-entrypoint.sh mqtt`` → ``python -m
fastapp.mqtt``. It is a SECOND transport over the transport-agnostic core in
``fastapp.ingest`` — no new business logic, no schema change; every message
persists byte-for-byte the same rows and enqueues the same alert tasks as the
equivalent ``POST /ingest/*`` route.

Why MQTT: ChirpStack (the LoRaWAN network server) already publishes every device
uplink to its built-in MQTT broker, so subscribing there is the native LoRaWAN
path (no ChirpStack HTTP-integration round-trip). Generic and Bivocom sensors
publish to a ``{prefix}/{client}/...`` topic tree on the same broker. MQTT is
push-based over a persistent connection, which survives the flaky rural links
these field devices sit behind far better than per-reading HTTP.

Topics (subscribed with per-filter callbacks so routing is exact):

  application/+/device/+/event/up   ChirpStack v4 uplink  → handle_chirpstack_uplink
  {prefix}/+/weather                multi-metric JSON     → handle_metrics
  {prefix}/+/sensor/+               single reading        → handle_metrics (one key)
  {prefix}/+/bivocom                bridge-shaped uplink  → handle_metrics (tags)

The ``client`` (username) and, for the sensor topic, the ``sensor_key`` come
from the topic wildcards. Every message is processed defensively: a bad payload
is logged and dropped — it never crashes the loop and never NAKs into a
redelivery storm. The broker connection auto-reconnects (``loop_forever``).

Additive by design: the HTTP ``/ingest/*`` routes keep working unchanged.
Roll back by simply stopping this container.

⚠️ Run exactly ONE instance. Scaling to N replicas makes each message process N
times (every subscriber gets every message) — double-writes and double-alerts.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
from typing import Any

import paho.mqtt.client as mqtt

from agri.core.database import session_scope
from agri.core.alerts import SENSOR_KEY_REGISTRY
from fastapp import ingest
from fastapp.routers.ingest import (
    _INGEST_SKIP_KEYS,
    ChirpStackUplink,
)
from fastapp.settings import get_settings

log = logging.getLogger("fastapp.mqtt")

# Reconnect backoff bounds (seconds) for loop_forever's built-in retry.
_RECONNECT_MIN = 1
_RECONNECT_MAX = 60


def _extract_metrics(payload: dict[str, Any]) -> dict[str, float]:
    """Keep only known, non-null ``sensor_key`` → value pairs — identical to the
    filter the HTTP weather route applies (registry-driven, skips ``npk``)."""
    return {
        key: payload[key]
        for key in SENSOR_KEY_REGISTRY
        if key in payload and payload[key] is not None and key not in _INGEST_SKIP_KEYS
    }


class MqttIngest:
    """Owns the paho client, the topic→handler wiring, and the run loop."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop = threading.Event()
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.settings.mqtt_client_id,
            clean_session=False,  # durable session: keep our QoS-1 subs across reconnects
        )

    # -- wiring --------------------------------------------------------------
    def _configure(self) -> None:
        s = self.settings
        if s.mqtt_username:
            self.client.username_pw_set(s.mqtt_username, s.mqtt_password or None)
        if s.mqtt_tls:
            self.client.tls_set()  # system CA store; broker cert must be trusted
        self.client.reconnect_delay_set(
            min_delay=_RECONNECT_MIN, max_delay=_RECONNECT_MAX
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_unrouted  # fallback for stray topics

        prefix = s.mqtt_topic_prefix.rstrip("/")
        # Per-filter callbacks → the concrete topic that arrives is matched to the
        # subscription that registered it, so routing needs no manual parsing.
        self.client.message_callback_add(s.mqtt_chirpstack_topic, self._on_chirpstack)
        self.client.message_callback_add(f"{prefix}/+/weather", self._on_weather)
        self.client.message_callback_add(f"{prefix}/+/sensor/+", self._on_sensor)
        self.client.message_callback_add(f"{prefix}/+/bivocom", self._on_bivocom)

    def _subscriptions(self) -> list[tuple[str, int]]:
        s = self.settings
        prefix = s.mqtt_topic_prefix.rstrip("/")
        qos = s.mqtt_qos
        return [
            (s.mqtt_chirpstack_topic, qos),
            (f"{prefix}/+/weather", qos),
            (f"{prefix}/+/sensor/+", qos),
            (f"{prefix}/+/bivocom", qos),
        ]

    # -- connection callbacks ------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("mqtt.connect_failed", extra={"reason_code": str(reason_code)})
            return
        subs = self._subscriptions()
        client.subscribe(subs)  # re-subscribe on every (re)connect
        log.info("mqtt.connected", extra={"topics": [t for t, _ in subs]})

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        # loop_forever auto-reconnects; just record why we dropped.
        log.warning("mqtt.disconnected", extra={"reason_code": str(reason_code)})

    def _on_unrouted(self, client, userdata, msg):
        log.warning("mqtt.unrouted_topic", extra={"topic": msg.topic})

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _load_json(msg) -> Any:
        """Decode a message body as JSON, or raise ValueError with context."""
        try:
            return json.loads(msg.payload or b"{}")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"non-JSON payload on {msg.topic}: {exc}") from exc

    def _guard(self, kind: str, topic: str, fn) -> None:
        """Run a message handler; log-and-drop anything it raises so one bad
        message never kills the subscriber loop."""
        try:
            fn()
        except ingest.IngestError as exc:
            log.warning(
                "mqtt.%s.rejected", kind, extra={"topic": topic, "error": exc.message}
            )
        except Exception:
            log.exception("mqtt.%s.failed", kind, extra={"topic": topic})

    # -- message handlers (one per topic filter) -----------------------------
    def _on_chirpstack(self, client, userdata, msg):
        def run():
            data = self._load_json(msg)
            payload = ChirpStackUplink.model_validate(data)
            rx = payload.rxInfo[0] if payload.rxInfo else None
            with session_scope(commit=True) as session:
                channels = ingest.handle_chirpstack_uplink(
                    session,
                    dev_eui=payload.deviceInfo.devEui,
                    device_name=payload.deviceInfo.deviceName or "",
                    f_cnt=payload.fCnt,
                    f_port=payload.fPort,
                    rssi=rx.rssi if rx else None,
                    snr=rx.snr if rx else None,
                    frequency=payload.txInfo.frequency if payload.txInfo else None,
                    obj=payload.object or {},
                    data=payload.data or "",
                )
            log.info(
                "mqtt.chirpstack.ok",
                extra={"devEui": payload.deviceInfo.devEui, "channels": channels},
            )

        self._guard("chirpstack", msg.topic, run)

    def _on_weather(self, client, userdata, msg):
        # topic: {prefix}/{client}/weather
        client_name = msg.topic.split("/")[1]

        def run():
            body = self._load_json(msg)
            if not isinstance(body, dict):
                raise ValueError("weather payload must be a JSON object")
            metrics = _extract_metrics(body)
            if not metrics:
                log.info("mqtt.weather.no_metrics", extra={"client": client_name})
                return
            with session_scope(commit=True) as session:
                inserted = ingest.handle_metrics(
                    session, client=client_name, metrics=metrics
                )
            log.info(
                "mqtt.weather.ok",
                extra={"client": client_name, "inserted": inserted},
            )

        self._guard("weather", msg.topic, run)

    def _on_sensor(self, client, userdata, msg):
        # topic: {prefix}/{client}/sensor/{sensor_key}
        parts = msg.topic.split("/")
        client_name, sensor_key = parts[1], parts[3]

        def run():
            if sensor_key not in SENSOR_KEY_REGISTRY or sensor_key in _INGEST_SKIP_KEYS:
                raise ingest.IngestError(
                    f"Unknown or unsupported sensor_key '{sensor_key}'"
                )
            body = self._load_json(msg)
            if not isinstance(body, dict) or "value" not in body:
                raise ValueError("sensor payload must be a JSON object with 'value'")
            value = float(body["value"])
            timestamp = ingest.parse_timestamp(body.get("timestamp"))
            with session_scope(commit=True) as session:
                ingest.handle_metrics(
                    session,
                    client=client_name,
                    metrics={sensor_key: value},
                    timestamp=timestamp,
                )
            log.info(
                "mqtt.sensor.ok",
                extra={"client": client_name, "sensor_key": sensor_key},
            )

        self._guard("sensor", msg.topic, run)

    def _on_bivocom(self, client, userdata, msg):
        # topic: {prefix}/{client}/bivocom
        # Body is the agri-bridge wire shape: {device_id, timestamp, tags:{k:v}}.
        # The bridge already emits canonical sensor_keys as tag names, so tags map
        # straight onto handle_metrics (same path as weather). Raw-Modbus-tag
        # Bivocom (via the DeviceSensor mapping) is a separate future step.
        client_name = msg.topic.split("/")[1]

        def run():
            body = self._load_json(msg)
            if not isinstance(body, dict):
                raise ValueError("bivocom payload must be a JSON object")
            tags = body.get("tags")
            if not isinstance(tags, dict):
                raise ValueError("bivocom payload missing 'tags' object")
            metrics = _extract_metrics(tags)
            if not metrics:
                log.info("mqtt.bivocom.no_metrics", extra={"client": client_name})
                return
            timestamp = ingest.parse_timestamp(body.get("timestamp"))
            with session_scope(commit=True) as session:
                inserted = ingest.handle_metrics(
                    session,
                    client=client_name,
                    metrics=metrics,
                    timestamp=timestamp,
                )
            log.info(
                "mqtt.bivocom.ok",
                extra={"client": client_name, "inserted": inserted},
            )

        self._guard("bivocom", msg.topic, run)

    # -- run loop ------------------------------------------------------------
    def run(self) -> None:
        s = self.settings
        if not s.mqtt_enabled:
            log.warning("mqtt.disabled — MQTT_HOST is empty; subscriber idling.")
            self._stop.wait()
            return

        self._configure()
        self._install_signal_handlers()
        log.info(
            "mqtt.starting",
            extra={"host": s.mqtt_host, "port": s.mqtt_port, "tls": s.mqtt_tls},
        )
        # connect_async + loop_forever gives us auto-reconnect from the first
        # attempt, so a broker that isn't up yet doesn't crash the process.
        self.client.connect_async(s.mqtt_host, s.mqtt_port, keepalive=60)
        self.client.loop_forever(retry_first_connection=True)

    def _install_signal_handlers(self) -> None:
        def _shutdown(signum, frame):
            log.info("mqtt.shutdown", extra={"signal": signum})
            self._stop.set()
            self.client.disconnect()  # breaks loop_forever cleanly

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)


def main() -> None:
    # Structured JSON so the extra={...} dicts this module already passes
    # (client, status, error body …) render as queryable keys in Loki, instead
    # of being dropped by an unconfigured formatter.
    from fastapp.logging_config import configure_logging
    from fastapp.settings import get_settings

    _s = get_settings()
    configure_logging(_s.log_level, _s.log_format)
    MqttIngest().run()


if __name__ == "__main__":
    main()
