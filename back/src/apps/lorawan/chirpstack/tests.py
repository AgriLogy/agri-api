"""Tests for the ChirpStack webhook (schema + pH decode + persistence)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from rest_framework.test import APIClient

from apps.lorawan.chirpstack.router import _decode_battery, _decode_ph
from apps.lorawan.chirpstack.schemas import ChirpStackUplink


def _valid_uplink_payload(**overrides) -> dict:
    payload = {
        "deviceInfo": {
            "devEui": "a840412ca45d64d0",
            "deviceName": "phh",
        },
        "rxInfo": [{"rssi": -111.0, "snr": -21.2, "gatewayId": "a84041ffff2b5d82"}],
        "fPort": 2,
        "fCnt": 574,
        "time": "2026-06-05T11:00:00Z",
        # raw RS485-LB data frame: 0D D4 01 02 F6  -> BatV 3.54, PayVer 1, pH 7.58
        "data": "DdQBAvY=",
        "object": {"BatV": 3.54, "Payver": 1.0, "Node_type": "RS485-LB"},
    }
    payload.update(overrides)
    return payload


# --------------------------- schema --------------------------------------


def test_schema_accepts_valid_uplink() -> None:
    p = ChirpStackUplink.model_validate(_valid_uplink_payload())
    assert p.deviceInfo.devEui == "a840412ca45d64d0"
    assert p.rxInfo[0].rssi == -111.0
    assert p.data == "DdQBAvY="


def test_schema_rejects_short_dev_eui() -> None:
    payload = _valid_uplink_payload()
    payload["deviceInfo"]["devEui"] = "TOO_SHORT"
    with pytest.raises(ValidationError):
        ChirpStackUplink.model_validate(payload)


def test_schema_ignores_extra_chirpstack_fields() -> None:
    payload = _valid_uplink_payload(someFutureField="ignored")
    p = ChirpStackUplink.model_validate(payload)
    assert p.deviceInfo.devEui == "a840412ca45d64d0"


# --------------------------- pH decode (pure) ----------------------------


def test_decode_ph_from_raw_data() -> None:
    """0x02F6 = 758 -> 7.58 (scaled /100), from the real captured frame."""
    p = ChirpStackUplink.model_validate(_valid_uplink_payload())
    assert _decode_ph(p) == 7.58


def test_decode_ph_prefers_codec_object_field() -> None:
    p = ChirpStackUplink.model_validate(
        _valid_uplink_payload(object={"pH": 6.81}, data=None)
    )
    assert _decode_ph(p) == 6.81


def test_decode_ph_status_frame_returns_none() -> None:
    # fPort 5 is the device-status frame — no measurement.
    p = ChirpStackUplink.model_validate(_valid_uplink_payload(fPort=5))
    assert _decode_ph(p) is None


def test_decode_ph_missing_data_returns_none() -> None:
    p = ChirpStackUplink.model_validate(_valid_uplink_payload(data=None, object={}))
    assert _decode_ph(p) is None


def test_decode_ph_out_of_range_rejected() -> None:
    # 0xFFFF -> 655.35, not a valid pH.
    p = ChirpStackUplink.model_validate(_valid_uplink_payload(data="DdQB//8="))
    assert _decode_ph(p) is None


# --------------------------- endpoint + persistence ----------------------


@pytest.mark.django_db
def test_uplink_persists_metrics_into_lora_zone() -> None:
    from analytics.models import BatterySensor, PhSoil, SignalSensor, Zone

    client = APIClient()
    resp = client.post(
        "/ingest/lorawan/chirpstack",
        data=_valid_uplink_payload(),
        format="json",
    )
    assert resp.status_code == 201
    # pH + battery + signal all present on this frame.
    assert resp.json() == {
        "accepted": True,
        "devEui": "a840412ca45d64d0",
        "channels": 3,
    }

    zone = Zone.objects.get(name="lora")
    assert zone.user.username == "lora"
    assert PhSoil.objects.get(zone=zone).value == 7.58
    assert BatterySensor.objects.get(zone=zone).value == 3.54
    assert SignalSensor.objects.get(zone=zone).value == -111.0


def test_decode_battery_from_data_frame() -> None:
    p = ChirpStackUplink.model_validate(_valid_uplink_payload())
    assert _decode_battery(p) == 3.54


def test_decode_battery_from_status_frame() -> None:
    # fPort 5 status frame reports battery as "BAT".
    p = ChirpStackUplink.model_validate(
        _valid_uplink_payload(fPort=5, object={"BAT": 3.48, "SENSOR_MODEL": "RS485-LB"})
    )
    assert _decode_battery(p) == 3.48


@pytest.mark.django_db
def test_uplink_stores_full_record() -> None:
    from apps.lorawan.chirpstack.models import LoraUplink

    payload = _valid_uplink_payload(txInfo={"frequency": 869300000})
    client = APIClient()
    resp = client.post("/ingest/lorawan/chirpstack", data=payload, format="json")
    assert resp.status_code == 201

    row = LoraUplink.objects.get(dev_eui="a840412ca45d64d0")
    assert row.battery_v == 3.54
    assert row.ph == 7.58
    assert row.f_cnt == 574
    assert row.f_port == 2
    assert row.rssi == -111.0
    assert row.frequency == 869300000
    assert row.raw_b64 == "DdQBAvY="
    assert row.decoded["Node_type"] == "RS485-LB"


@pytest.mark.django_db
def test_status_frame_stores_battery_signal_but_no_ph() -> None:
    from analytics.models import BatterySensor, PhSoil, SignalSensor
    from apps.lorawan.chirpstack.models import LoraUplink

    client = APIClient()
    resp = client.post(
        "/ingest/lorawan/chirpstack",
        data=_valid_uplink_payload(
            fPort=5, object={"BAT": 3.48, "SENSOR_MODEL": "RS485-LB"}, data=None
        ),
        format="json",
    )
    # No pH on a status frame, but battery + signal are still graphed.
    assert resp.status_code == 201
    assert resp.json()["channels"] == 2
    assert not PhSoil.objects.exists()
    assert BatterySensor.objects.first().value == 3.48
    assert SignalSensor.objects.first().value == -111.0
    # …and the full uplink is captured too.
    row = LoraUplink.objects.get(dev_eui="a840412ca45d64d0")
    assert row.battery_v == 3.48
    assert row.ph is None


@pytest.mark.django_db
def test_uplink_endpoint_rejects_invalid() -> None:
    client = APIClient()
    resp = client.post(
        "/ingest/lorawan/chirpstack",
        data={"deviceInfo": {"devEui": "TOO_SHORT"}},
        format="json",
    )
    assert resp.status_code == 422
