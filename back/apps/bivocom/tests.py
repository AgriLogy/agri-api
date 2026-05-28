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


@pytest.mark.django_db
def test_uplink_endpoint_accepts_valid() -> None:
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
    assert body == {"accepted": True, "device_id": "BV-001", "tag_count": 2}


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
