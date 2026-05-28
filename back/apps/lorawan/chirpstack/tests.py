"""Tests for the ChirpStack webhook."""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from rest_framework.test import APIClient

from apps.lorawan.chirpstack.schemas import ChirpStackUplink


def _valid_uplink_payload() -> dict:
    return {
        "deviceInfo": {
            "devEui": "0011223344556677",
            "deviceName": "soil-sensor-01",
        },
        "rxInfo": [{"rssi": -85.2, "snr": 7.5, "gatewayId": "gw-001"}],
        "fPort": 1,
        "fCnt": 42,
        "time": "2026-05-28T11:00:00Z",
        "object": {
            "airTemperature": {"value": 22.5, "unit": "C"},
            "soilMoisture": {"value": 0.32, "unit": "m3/m3"},
        },
    }


def test_schema_accepts_valid_uplink() -> None:
    p = ChirpStackUplink.model_validate(_valid_uplink_payload())
    assert p.deviceInfo.devEui == "0011223344556677"
    assert p.rxInfo[0].rssi == -85.2
    assert len(p.object) == 2


def test_schema_rejects_short_dev_eui() -> None:
    payload = _valid_uplink_payload()
    payload["deviceInfo"]["devEui"] = "TOO_SHORT"
    with pytest.raises(ValidationError):
        ChirpStackUplink.model_validate(payload)


def test_schema_rejects_missing_device_info() -> None:
    with pytest.raises(ValidationError):
        ChirpStackUplink.model_validate({"rxInfo": []})


def test_schema_ignores_extra_chirpstack_fields() -> None:
    """ChirpStack adds new fields over time; we must NOT 400 on them."""
    payload = _valid_uplink_payload()
    payload["someFutureField"] = "ignored"
    payload["deviceInfo"]["newField"] = "ignored"
    p = ChirpStackUplink.model_validate(payload)
    assert p.deviceInfo.devEui == "0011223344556677"


@pytest.mark.django_db
def test_uplink_endpoint_accepts_valid() -> None:
    client = APIClient()
    resp = client.post(
        "/api/v1/lorawan/chirpstack/uplink",
        data=_valid_uplink_payload(),
        format="json",
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body == {"accepted": True, "devEui": "0011223344556677", "channels": 2}


@pytest.mark.django_db
def test_uplink_endpoint_rejects_invalid() -> None:
    client = APIClient()
    resp = client.post(
        "/api/v1/lorawan/chirpstack/uplink",
        data={"deviceInfo": {"devEui": "TOO_SHORT"}},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation_error"
