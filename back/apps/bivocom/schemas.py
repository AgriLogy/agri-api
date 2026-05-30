"""Pydantic schemas for the Bivocom uplink endpoint.

Per the senior-dev guideline, every API contract is a pydantic
``BaseModel`` — never raw dicts, never untyped JSON. The view validates
inbound payloads against ``BivocomUplink`` (rejects bad shape with 400)
and emits ``BivocomUplinkResponse`` (consistent shape for callers).

The actual mapping ``tag_name`` → ``SensorKind`` lives in the
``BivocomAdapter`` in ``agri-core`` (Phase 6.5). This module only
covers the HTTP wire format.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BivocomUplink(BaseModel):
    """Bivocom HTTP-push payload.

    Wire shape::

        POST /api/v1/bivocom/uplink
        Content-Type: application/json
        {
          "device_id": "BV-001",
          "timestamp": "2026-05-28T11:00:00Z",
          "rssi": -78.5,
          "tags": {"ta": 22.5, "ha": 64.0, "ms": 0.32}
        }
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: str = Field(
        min_length=1, max_length=64, description="Bivocom device identifier"
    )
    timestamp: datetime = Field(
        description="Reading timestamp (ISO-8601, tz-aware preferred)"
    )
    rssi: float | None = Field(
        default=None, description="Signal strength in dBm, if reported"
    )
    tags: dict[str, float] = Field(
        description="Modbus-tag → value map (e.g. ta=air temp °C, ms=soil moisture m³/m³)",
        min_length=1,
    )


class BivocomUplinkResponse(BaseModel):
    """Response shape after a successful ingest."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool = True
    device_id: str
    tag_count: int = Field(ge=0, description="Number of tag readings accepted")
