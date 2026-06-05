"""Pydantic schemas for the ChirpStack v4 HTTP integration webhook.

ChirpStack v4 reference (Uplink event):
  https://www.chirpstack.io/docs/chirpstack/integrations/events.html#uplink-event

We model the *subset* we actually consume — extra fields from ChirpStack are
allowed (``extra='ignore'``) so adding fields upstream doesn't break us.

The decoded sensor payload arrives in the ``object`` field as
ChirpStack-decoded JSON; its concrete shape depends on the device's
Cayenne codec. Validation of that nested shape is intentionally LOOSE
at this layer (just ``dict[str, Any]``) — the per-channel mapping to
``SensorKind`` lives in ``agri.core.devices.lorawan.chirpstack.ChirpStackAdapter``
(Phase 6.5), not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChirpStackDeviceInfo(BaseModel):
    """Identifying info about the LoRaWAN device emitting the uplink."""

    model_config = ConfigDict(extra="ignore")

    devEui: str = Field(
        min_length=16, max_length=16, description="64-bit DevEUI as 16 hex chars"
    )
    deviceName: str | None = None
    applicationName: str | None = None
    tenantName: str | None = None


class ChirpStackRxInfo(BaseModel):
    """Radio reception info from one gateway (uplinks may include multiple)."""

    model_config = ConfigDict(extra="ignore")

    rssi: float | None = None
    snr: float | None = None
    gatewayId: str | None = None


class ChirpStackUplink(BaseModel):
    """ChirpStack v4 HTTP-integration uplink event (subset)."""

    model_config = ConfigDict(extra="ignore")

    deviceInfo: ChirpStackDeviceInfo
    rxInfo: list[ChirpStackRxInfo] = Field(default_factory=list)
    fPort: int | None = None
    fCnt: int | None = None
    time: datetime | None = None
    data: str | None = Field(
        default=None,
        description="Raw uplink payload, base64-encoded (used to decode a "
        "value the codec doesn't expose yet)",
    )
    object: dict[str, Any] = Field(
        default_factory=dict,
        description="Codec-decoded sensor payload; shape depends on the device codec",
    )


class ChirpStackUplinkResponse(BaseModel):
    """Response shape after accepting a ChirpStack uplink event."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool = True
    devEui: str
    channels: int = Field(ge=0, description="Number of decoded channels accepted")
