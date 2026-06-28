"""Tests for the Bivocom ingest endpoint."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.bivocom.schemas import BivocomUplink


def test_schema_accepts_valid_payload() -> None:
    """Sanity check the pydantic schema."""
    payload = BivocomUplink.model_validate(
        {
            "device_id": "BV-001",
            "timestamp": "2026-05-28T11:00:00Z",
            "rssi": -78.5,
            "tags": {"ta": 22.5, "ms": 0.32},
        }
    )
    assert payload.device_id == "BV-001"
    assert payload.tags["ta"] == 22.5
    assert payload.rssi == -78.5


def test_schema_rejects_empty_tags() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BivocomUplink.model_validate(
            {
                "device_id": "BV-001",
                "timestamp": "2026-05-28T11:00:00Z",
                "tags": {},
            }
        )


def test_schema_rejects_empty_device_id() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BivocomUplink.model_validate(
            {
                "device_id": "",
                "timestamp": "2026-05-28T11:00:00Z",
                "tags": {"ta": 22.5},
            }
        )


def _register(serial="BV-001", *, active=True, with_owner=True):
    """Create a user + zone + bivocom device for ingest tests."""
    from django.contrib.auth import get_user_model

    from apps.irrigation.models import Device
    from analytics.models import Zone

    User = get_user_model()
    user = User.objects.create(username=f"owner-{serial}", email=f"{serial}@x.io")
    zone = Zone.objects.create(
        user=user,
        name=f"zone-{serial}",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )
    device = Device.objects.create(
        device_type="bivocom",
        serial=serial,
        user=user if with_owner else None,
        zone=zone,
        is_active=active,
    )
    return user, zone, device


@pytest.mark.django_db
def test_uplink_unknown_device_is_404() -> None:
    client = APIClient()
    resp = client.post(
        "/ingest/bivocom",
        data={
            "device_id": "BV-UNKNOWN",
            "timestamp": "2026-05-28T11:00:00Z",
            "tags": {"ta": 22.5},
        },
        format="json",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_uplink_maps_tags_and_skips_unmapped() -> None:
    from apps.alerts.engine import get_sensor_model
    from apps.irrigation.models import DeviceSensor

    user, zone, device = _register("BV-001")
    # Only 'ta' is mapped; 'ms' has no attachment → must be skipped.
    DeviceSensor.objects.create(
        device=device, tag_name="ta", sensor_key="temperature_weather"
    )

    client = APIClient()
    resp = client.post(
        "/ingest/bivocom",
        data={
            "device_id": "BV-001",
            "timestamp": "2026-05-28T11:00:00Z",
            "rssi": -78.5,
            "tags": {"ta": 22.5, "ms": 0.32},
        },
        format="json",
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body == {"accepted": True, "device_id": "BV-001", "tag_count": 1}

    # The mapped reading landed in the right table, scoped to the device's zone.
    model_cls = get_sensor_model("temperature_weather")
    rows = list(model_cls.objects.filter(zone=zone))
    assert len(rows) == 1
    assert rows[0].value == 22.5
    assert rows[0].user_id == user.id


@pytest.mark.django_db
def test_uplink_sensor_zone_override_wins() -> None:
    from apps.alerts.engine import get_sensor_model
    from apps.irrigation.models import DeviceSensor
    from analytics.models import Zone

    user, device_zone, device = _register("BV-OVR")
    other_zone = Zone.objects.create(
        user=user,
        name="override-zone",
        space=500.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )
    DeviceSensor.objects.create(
        device=device,
        tag_name="ta",
        sensor_key="temperature_weather",
        zone=other_zone,
    )
    client = APIClient()
    resp = client.post(
        "/ingest/bivocom",
        data={
            "device_id": "BV-OVR",
            "timestamp": "2026-05-28T11:00:00Z",
            "tags": {"ta": 19.0},
        },
        format="json",
    )
    assert resp.status_code == 202
    model_cls = get_sensor_model("temperature_weather")
    assert model_cls.objects.filter(zone=other_zone).count() == 1
    assert model_cls.objects.filter(zone=device_zone).count() == 0


@pytest.mark.django_db
def test_uplink_inactive_device_is_422() -> None:
    _register("BV-OFF", active=False)
    client = APIClient()
    resp = client.post(
        "/ingest/bivocom",
        data={
            "device_id": "BV-OFF",
            "timestamp": "2026-05-28T11:00:00Z",
            "tags": {"ta": 22.5},
        },
        format="json",
    )
    assert resp.status_code == 422


@pytest.mark.django_db
def test_uplink_endpoint_rejects_invalid() -> None:
    client = APIClient()
    resp = client.post(
        "/ingest/bivocom",
        data={"device_id": "BV-001"},  # missing timestamp + tags
        format="json",
    )
    # django-ninja returns 422 + {"detail": [...]} for pydantic
    # ValidationError; the Bivocom gateway treats any 4xx as "stop
    # retrying", which matches the legacy DRF 400 behavior.
    assert resp.status_code == 422
    assert "detail" in resp.json()
