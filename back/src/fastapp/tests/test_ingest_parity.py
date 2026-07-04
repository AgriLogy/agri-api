"""F9-ingest golden parity: device webhooks — the fastapp sidecar must match
the django-ninja ingest routes byte-for-byte on the response envelope AND
persist the same rows + enqueue the same alert tasks.

Covered surfaces (all ``auth=None`` — device shared-secret, not JWT):
  * POST /ingest/bivocom            (stub: validate + 202, no persist)
  * POST /ingest/lorawan/chirpstack (decode pH/battery/rssi → LoraUplink +
                                     per-metric rows under the ``lora`` zone)
  * POST /ingest/weather            (multi-sensor registry-driven ingest)
  * POST /ingest/sensor             (single typed reading + alert dispatch)

Dual-ORM: Postgres only + committed rows (fastapp writes/reads over a separate
SQLAlchemy connection). Alert dispatch is proved by monkeypatching BOTH the
Django task ``.delay`` and ``fastapp.celery.send_task`` to no-ops and asserting
the same task name + kwargs — no broker needed.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework.test import APIClient

from fastapp import celery
from fastapp.main import app

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def django() -> APIClient:
    return APIClient()


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="ingest-owner",
        email="ingest-owner@example.com",
        password="irrelevant-3921",
    )


@pytest.fixture
def zone(owner):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=owner,
        name="Parcelle I",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _rows(model_cls, **filt):
    return list(model_cls.objects.filter(**filt).values_list("value", flat=True))


# ---------------------------------------------------------------------------
# Bivocom (stub) — validate + 202, byte-identical envelope, no persistence.
# ---------------------------------------------------------------------------
def test_bivocom_uplink_parity(fast, django):
    body = {
        "device_id": "BV-001",
        "timestamp": "2026-05-28T11:00:00Z",
        "rssi": -78.5,
        "tags": {"ta": 22.5, "ha": 64.0, "ms": 0.32},
    }
    dj = django.post("/ingest/bivocom", body, format="json")
    fp = fast.post("/ingest/bivocom", json=body)
    assert dj.status_code == fp.status_code == 202, (dj.content, fp.text)
    assert dj.content == fp.content
    assert fp.json() == {"accepted": True, "device_id": "BV-001", "tag_count": 3}


# ---------------------------------------------------------------------------
# ChirpStack — data frame: LoraUplink + 3 per-metric rows, 201 channels=3.
# ---------------------------------------------------------------------------
def test_chirpstack_data_frame_parity(fast, django):
    from apps.lorawan.chirpstack.models import LoraUplink

    body = {
        "deviceInfo": {"devEui": "a1b2c3d4e5f60718", "deviceName": "probe-1"},
        "rxInfo": [{"rssi": -80.0, "snr": 9.2}],
        "txInfo": {"frequency": 868100000},
        "fPort": 2,
        "fCnt": 11,
        "object": {"pH": 7.5, "BatV": 3.6},
    }
    dj = django.post("/ingest/lorawan/chirpstack", body, format="json")
    fp = fast.post("/ingest/lorawan/chirpstack", json=body)

    assert dj.status_code == fp.status_code == 201, (dj.content, fp.text)
    assert dj.content == fp.content  # byte-parity
    assert fp.json() == {"accepted": True, "devEui": "a1b2c3d4e5f60718", "channels": 3}

    # Each surface appended a complete raw-uplink record (append-only).
    uplinks = list(
        LoraUplink.objects.filter(dev_eui="a1b2c3d4e5f60718").values_list(
            "ph", "battery_v"
        )
    )
    assert uplinks == [(7.5, 3.6), (7.5, 3.6)]

    # Per-metric graph rows written under the shared ``lora`` zone: 2 of each
    # (one per surface), same values.
    from apps.alerts.engine import get_sensor_model

    assert sorted(_rows(get_sensor_model("ph_soil"))) == [7.5, 7.5]
    assert sorted(_rows(get_sensor_model("battery"))) == [3.6, 3.6]
    assert sorted(_rows(get_sensor_model("signal"))) == [-80.0, -80.0]


# ---------------------------------------------------------------------------
# ChirpStack — status frame (fPort 5): no readings → 202 channels=0.
# ---------------------------------------------------------------------------
def test_chirpstack_status_frame_parity(fast, django):
    body = {
        "deviceInfo": {"devEui": "b1b2c3d4e5f60718"},
        "rxInfo": [],
        "fPort": 5,
        "fCnt": 3,
        "object": {},
    }
    dj = django.post("/ingest/lorawan/chirpstack", body, format="json")
    fp = fast.post("/ingest/lorawan/chirpstack", json=body)
    assert dj.status_code == fp.status_code == 202, (dj.content, fp.text)
    assert dj.content == fp.content
    assert fp.json() == {"accepted": True, "devEui": "b1b2c3d4e5f60718", "channels": 0}


# ---------------------------------------------------------------------------
# Weather multi-sensor ingest.
# ---------------------------------------------------------------------------
def test_weather_ingest_parity(fast, django, owner, zone):
    from apps.alerts.engine import get_sensor_model

    body = {
        "client": "ingest-owner",
        "temperature_weather": 21.5,
        "humidity_weather": 63.0,
    }
    dj = django.post("/ingest/weather", body, format="json")
    fp = fast.post("/ingest/weather", json=body)
    assert dj.status_code == fp.status_code == 201, (dj.content, fp.text)
    assert dj.content == fp.content
    assert fp.json() == {"inserted": 2}

    # Two rows per metric (one per surface), same values, owner's zone.
    assert sorted(_rows(get_sensor_model("temperature_weather"), zone=zone)) == [
        21.5,
        21.5,
    ]
    assert sorted(_rows(get_sensor_model("humidity_weather"), zone=zone)) == [
        63.0,
        63.0,
    ]


def test_weather_all_metrics_none_parity(fast, django, owner, zone):
    body = {"client": "ingest-owner"}  # no registry metric keys present
    dj = django.post("/ingest/weather", body, format="json")
    fp = fast.post("/ingest/weather", json=body)
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    assert dj.content == fp.content
    assert fp.json() == {"inserted": 0, "detail": "all_metrics_none"}


def test_weather_user_not_found_400_parity(fast, django):
    body = {"client": "ghost-user", "temperature_weather": 20.0}
    dj = django.post("/ingest/weather", body, format="json")
    fp = fast.post("/ingest/weather", json=body)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content
    assert fp.json() == {"error": "User not found for client 'ghost-user'"}


def test_weather_no_zone_400_parity(fast, django, owner):
    # owner exists but has no zone
    body = {"client": "ingest-owner", "temperature_weather": 20.0}
    dj = django.post("/ingest/weather", body, format="json")
    fp = fast.post("/ingest/weather", json=body)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content
    assert fp.json() == {"error": "No zone found for user 'ingest-owner'"}


# ---------------------------------------------------------------------------
# Single-sensor ingest + alert dispatch parity (same task, same kwargs).
# ---------------------------------------------------------------------------
@pytest.fixture
def _capture(monkeypatch):
    """Record enqueues on BOTH surfaces: fastapp.celery.send_task and the
    Django Celery task ``.delay`` methods. Returns (fast_calls, dj_calls)."""
    import agriapi.tasks as tasks

    fast_calls: list[tuple[str, dict]] = []
    dj_calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        celery, "send_task", lambda name, **kw: fast_calls.append((name, kw))
    )
    for tname in (
        "send_alert_email",
        "send_alert_digest_email",
        "send_alert_whatsapp",
        "send_alert_sms",
    ):
        task = getattr(tasks, tname)
        full = f"agriapi.tasks.{tname}"
        monkeypatch.setattr(
            task,
            "delay",
            (lambda full: lambda **kw: dj_calls.append((full, kw)))(full),
        )
    return fast_calls, dj_calls


def test_sensor_ingest_and_alert_dispatch_parity(fast, django, owner, zone, _capture):
    from apps.alerts.engine import get_sensor_model
    from apps.alerts.models import Alert

    fast_calls, dj_calls = _capture

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

    body = {
        "client": "ingest-owner",
        "sensor_key": "soil_ph",
        "value": 8.5,
        "timestamp": "2026-06-01T09:00:00+00:00",
    }

    # Django side.
    dj = django.post("/ingest/sensor", body, format="json")
    assert dj.status_code == 201, dj.content

    # Reset the atomic grace claim so fastapp faces the same fresh-alert state
    # (both surfaces share one DB — the first claim would otherwise block the
    # second within the grace window).
    Alert.objects.filter(pk=alert.pk).update(
        last_emailed_at=None, last_triggered_at=None
    )

    # fastapp side.
    fp = fast.post("/ingest/sensor", json=body)
    assert fp.status_code == 201, fp.text

    # Byte-identical response envelope.
    assert dj.content == fp.content
    assert fp.json() == {"inserted": 1, "sensor_key": "soil_ph"}

    # Both persisted the reading (one row each), same value + zone.
    assert sorted(_rows(get_sensor_model("soil_ph"), zone=zone)) == [8.5, 8.5]

    # Same single alert-email enqueue on both surfaces, identical kwargs
    # (alert_id / value / timestamp_iso — the explicit ts makes it exact).
    expected_kwargs = {
        "alert_id": alert.pk,
        "value": 8.5,
        "timestamp_iso": "2026-06-01T09:00:00+00:00",
    }
    assert dj_calls == [("agriapi.tasks.send_alert_email", expected_kwargs)]
    assert fast_calls == [("agriapi.tasks.send_alert_email", expected_kwargs)]
