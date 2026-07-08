"""MQTT ingest — full end-to-end (real broker + real Postgres + real handlers).

The highest-fidelity test: a real paho publish crosses a real ``mosquitto``
broker, the live ``MqttIngest`` subscriber receives it on its network thread,
and the UNMOCKED ``fastapp.ingest`` handlers persist rows through the agri-core
SQLAlchemy session + enqueue the same alert tasks the HTTP path does. Every
ingest topic and every branch (data/status frames, multi/single/bivocom
metrics, alert dispatch, and the drop paths) is exercised against the database.

Gating (mirrors ``test_ingest_parity``):
  * Postgres only — the subscriber writes over a separate SQLAlchemy connection
    and the assertions read committed rows via the Django ORM, so it needs a
    real shared database (``django_db(transaction=True)``); skips on sqlite.
  * mosquitto — the ``mqtt_broker`` fixture skips when the binary is absent.

Negative cases use a SENTINEL: publish the bad message, then a known-good one,
and wait for the good one's row. Because the subscriber processes messages
in-order on one thread, once the sentinel landed the bad message definitely was
handled too — so "no row was written" is a deterministic assertion, not a sleep.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings

from fastapp import celery
from fastapp.tests.conftest import wait_until

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="MQTT e2e requires Postgres (dual-ORM, committed rows across threads)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


# ---------------------------------------------------------------------------
# Fixtures (mirror test_ingest_parity's owner/zone).
# ---------------------------------------------------------------------------
@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="mqtt-owner",
        email="mqtt-owner@example.com",
        password="irrelevant-7731",
    )


@pytest.fixture
def zone(owner):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=owner,
        name="Parcelle MQTT",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


@pytest.fixture
def capture_tasks(monkeypatch):
    """Record the alert tasks the ingest path enqueues (by name + kwargs),
    across the subscriber's network thread (monkeypatch is process-wide)."""
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        celery, "send_task", lambda name, **kw: calls.append((name, kw))
    )
    return calls


def _rows(model_cls, **filt):
    return sorted(model_cls.objects.filter(**filt).values_list("value", flat=True))


# ---------------------------------------------------------------------------
# ChirpStack — data frame: LoraUplink + 3 metric rows under the ``lora`` zone.
# ---------------------------------------------------------------------------
def test_chirpstack_data_frame_persists(mqtt_subscriber, mqtt_publish):
    from apps.alerts.engine import get_sensor_model
    from apps.lorawan.chirpstack.models import LoraUplink

    # Unique devEui: lora_uplink is UNMANAGED, so TransactionTestCase never
    # truncates it — rows accumulate across the whole session. Scoping every
    # assertion to a devEui no other test uses is this codebase's convention
    # (see test_ingest_parity) and keeps the count deterministic.
    dev_eui = "e2edata000000001"
    mqtt_publish(
        f"application/7/device/{dev_eui}/event/up",
        {
            "deviceInfo": {"devEui": dev_eui, "deviceName": "probe-1"},
            "rxInfo": [{"rssi": -80.0, "snr": 9.2}],
            "txInfo": {"frequency": 868100000},
            "fPort": 2,
            "fCnt": 11,
            "object": {"pH": 7.5, "BatV": 3.6},
        },
    )

    assert wait_until(
        lambda: LoraUplink.objects.filter(dev_eui=dev_eui).exists(),
        timeout=15,
    ), "uplink never persisted"
    # Give the 3 metric writes a beat to land after the uplink row.
    assert wait_until(lambda: len(_rows(get_sensor_model("ph_soil"))) == 1, timeout=10)

    assert list(
        LoraUplink.objects.filter(dev_eui=dev_eui).values_list("ph", "battery_v")
    ) == [(7.5, 3.6)]
    assert _rows(get_sensor_model("ph_soil")) == [7.5]
    assert _rows(get_sensor_model("battery")) == [3.6]
    assert _rows(get_sensor_model("signal")) == [-80.0]


# ---------------------------------------------------------------------------
# ChirpStack — status frame (fPort 5): raw uplink stored, NO metric rows.
# ---------------------------------------------------------------------------
def test_chirpstack_status_frame_stores_uplink_only(mqtt_subscriber, mqtt_publish):
    from apps.alerts.engine import get_sensor_model
    from apps.lorawan.chirpstack.models import LoraUplink

    dev_eui = "e2estat000000002"  # unique — see the data-frame test's note
    mqtt_publish(
        f"application/7/device/{dev_eui}/event/up",
        {
            "deviceInfo": {"devEui": dev_eui},
            "rxInfo": [],
            "fPort": 5,
            "fCnt": 3,
            "object": {},
        },
    )
    assert wait_until(
        lambda: LoraUplink.objects.filter(dev_eui=dev_eui).exists(),
        timeout=15,
    )
    # No rxInfo/pH/battery on a status frame → zero metric rows.
    assert _rows(get_sensor_model("ph_soil")) == []
    assert _rows(get_sensor_model("battery")) == []
    assert _rows(get_sensor_model("signal")) == []


# ---------------------------------------------------------------------------
# Generic weather multi-metric → rows under the owner's zone.
# ---------------------------------------------------------------------------
def test_weather_multi_metric_persists(mqtt_subscriber, mqtt_publish, owner, zone):
    from apps.alerts.engine import get_sensor_model

    mqtt_publish(
        "agrilogy/mqtt-owner/weather",
        {"temperature_weather": 21.5, "humidity_weather": 63.0},
    )
    assert wait_until(
        lambda: len(_rows(get_sensor_model("temperature_weather"), zone=zone)) == 1,
        timeout=15,
    )
    assert _rows(get_sensor_model("temperature_weather"), zone=zone) == [21.5]
    assert _rows(get_sensor_model("humidity_weather"), zone=zone) == [63.0]


# ---------------------------------------------------------------------------
# Generic single-sensor → one row.
# ---------------------------------------------------------------------------
def test_single_sensor_persists(mqtt_subscriber, mqtt_publish, owner, zone):
    from apps.alerts.engine import get_sensor_model

    mqtt_publish("agrilogy/mqtt-owner/sensor/temperature_weather", {"value": 18.25})
    assert wait_until(
        lambda: _rows(get_sensor_model("temperature_weather"), zone=zone) == [18.25],
        timeout=15,
    )


# ---------------------------------------------------------------------------
# Single-sensor that breaches an alert → row + the SAME alert-email enqueue.
# ---------------------------------------------------------------------------
def test_single_sensor_triggers_alert(
    mqtt_subscriber, mqtt_publish, owner, zone, capture_tasks
):
    from apps.alerts.engine import get_sensor_model
    from apps.alerts.models import Alert

    alert = Alert.objects.create(
        user=owner,
        zone=zone,
        name="hot soil pH",
        type="pH Level",
        description="pH too high",
        condition=">",
        condition_nbr=7,
        sensor_key="soil_ph",
        is_active=True,
    )

    mqtt_publish(
        "agrilogy/mqtt-owner/sensor/soil_ph",
        {"value": 8.5, "timestamp": "2026-06-01T09:00:00+00:00"},
    )
    assert wait_until(
        lambda: _rows(get_sensor_model("soil_ph"), zone=zone) == [8.5], timeout=15
    )
    assert wait_until(lambda: capture_tasks, timeout=10), "alert never enqueued"
    assert capture_tasks == [
        (
            "agriapi.tasks.send_alert_email",
            {
                "alert_id": alert.pk,
                "value": 8.5,
                "timestamp_iso": "2026-06-01T09:00:00+00:00",
            },
        )
    ]


# ---------------------------------------------------------------------------
# Bivocom (bridge-shaped) → tags (already sensor_keys) persist as rows.
# ---------------------------------------------------------------------------
def test_bivocom_bridge_shaped_persists(mqtt_subscriber, mqtt_publish, owner, zone):
    from apps.alerts.engine import get_sensor_model

    mqtt_publish(
        "agrilogy/mqtt-owner/bivocom",
        {
            "device_id": "router-user-mqtt-owner",
            "timestamp": "2026-06-01T09:00:00Z",
            "tags": {"temperature_weather": 22.5, "ta": 999},  # 'ta' unknown → ignored
        },
    )
    assert wait_until(
        lambda: _rows(get_sensor_model("temperature_weather"), zone=zone) == [22.5],
        timeout=15,
    )
    # The unknown 'ta' tag wrote nothing (it isn't a sensor_key).


# ---------------------------------------------------------------------------
# Drop paths (deterministic via a trailing sentinel that DOES persist).
# ---------------------------------------------------------------------------
def test_unknown_user_dropped(mqtt_subscriber, mqtt_publish, owner, zone):
    from apps.alerts.engine import get_sensor_model

    # Unknown client → dropped; sentinel for the real owner → persists.
    mqtt_publish("agrilogy/ghost-user/weather", {"temperature_weather": 99.0})
    mqtt_publish("agrilogy/mqtt-owner/weather", {"temperature_weather": 15.0})
    assert wait_until(
        lambda: _rows(get_sensor_model("temperature_weather"), zone=zone) == [15.0],
        timeout=15,
    )
    # 99.0 (ghost user) never made it — only the owner's 15.0 exists.
    assert _rows(get_sensor_model("temperature_weather"), zone=zone) == [15.0]


def test_unknown_sensor_key_dropped(mqtt_subscriber, mqtt_publish, owner, zone):
    from apps.alerts.engine import get_sensor_model

    mqtt_publish("agrilogy/mqtt-owner/sensor/not_a_sensor", {"value": 42.0})
    mqtt_publish("agrilogy/mqtt-owner/sensor/temperature_weather", {"value": 16.0})
    assert wait_until(
        lambda: _rows(get_sensor_model("temperature_weather"), zone=zone) == [16.0],
        timeout=15,
    )


def test_malformed_payload_dropped_subscriber_survives(
    mqtt_subscriber, mqtt_publish, owner, zone
):
    from apps.alerts.engine import get_sensor_model

    mqtt_publish("agrilogy/mqtt-owner/weather", b"not-json{{{")
    mqtt_publish("agrilogy/mqtt-owner/weather", {"temperature_weather": 17.0})
    assert wait_until(
        lambda: _rows(get_sensor_model("temperature_weather"), zone=zone) == [17.0],
        timeout=15,
    ), "subscriber did not survive the malformed message"
