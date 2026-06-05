"""ChirpStack v4 webhook router (django-ninja).

``POST /api/v1/lorawan/chirpstack/uplink`` — the ChirpStack v4 HTTP
integration target.

Decodes the pH reading from the Dragino RS485-LB uplink and persists it as a
``PhSoil`` row under the dedicated ``lora`` zone — so every LoRaWAN device is
grouped under one zone and shows on the dashboard pH graph, reusing the same
per-sensor write the live weather/Bivocom ingest uses.

The future ``SensorReading[]`` ingest controller (Phase 6.5) will own this;
for now we persist directly via the ORM, which is exactly what the weather
ingest does today. No schema change — the ``lora`` zone/user are created as
rows on first use (schema-of-record stays in agri-db).

Auth: ``auth=None`` — the integration calls in unauthenticated (shared
network / secret header; TODO enforce in a follow-up).
"""

from __future__ import annotations

import base64
import logging

from django.utils import timezone
from ninja import Router
from ninja.responses import Response

from analytics.models import PhSoil, Zone
from apps.lorawan.chirpstack.schemas import (
    ChirpStackUplink,
    ChirpStackUplinkResponse,
)
from apps.users.models import CustomUser

router = Router()
log = logging.getLogger("lorawan.chirpstack")

#: Every LoRaWAN reading is grouped under this single zone / owner.
LORA_ZONE_NAME = "lora"
LORA_USER_NAME = "lora"

#: The pH meter reports a 16-bit integer scaled ×100 (0x02F6 = 758 = 7.58).
_PH_SCALE = 100.0
_STATUS_FPORT = 5  # RS485-LB device-status frame — carries no measurement


def _decode_ph(payload: ChirpStackUplink) -> float | None:
    """Extract the pH value from an RS485-LB uplink.

    Prefers a codec-decoded ``pH`` field; otherwise reads it straight from the
    raw payload. Data frames are ``[BatV:2][PayVer:1][modbus bytes...]`` and the
    pH meter returns a 16-bit value scaled ×100. Status frames (fPort 5) carry
    no measurement.
    """
    obj = payload.object or {}
    for key in ("pH", "ph", "PH", "soil_ph", "ph_soil"):
        value = obj.get(key)
        if isinstance(value, (int, float)):
            ph = round(float(value), 2)
            return ph if 0.0 <= ph <= 14.0 else None

    if payload.fPort == _STATUS_FPORT or not payload.data:
        return None
    try:
        raw = base64.b64decode(payload.data)
    except (ValueError, TypeError):
        return None
    if len(raw) < 5:
        return None
    ph = ((raw[3] << 8) | raw[4]) / _PH_SCALE
    return round(ph, 2) if 0.0 <= ph <= 14.0 else None


def _lora_zone() -> Zone:
    """Resolve (and lazily provision) the dedicated ``lora`` zone."""
    user, _ = CustomUser.objects.get_or_create(
        username=LORA_USER_NAME,
        defaults={"email": "lora@local.invalid"},
    )
    zone, _ = Zone.objects.get_or_create(
        name=LORA_ZONE_NAME,
        defaults={
            "user": user,
            "space": 1.0,
            "critical_moisture_threshold": 20.0,
        },
    )
    return zone


@router.post(
    "",
    response={201: ChirpStackUplinkResponse, 202: ChirpStackUplinkResponse, 400: dict},
    auth=None,
    summary="ChirpStack v4 uplink webhook (POST /ingest/lorawan/chirpstack)",
)
def chirpstack_uplink(request, payload: ChirpStackUplink):
    dev_eui = payload.deviceInfo.devEui
    ph = _decode_ph(payload)
    log.info(
        "chirpstack.uplink",
        extra={"dev_eui": dev_eui, "fPort": payload.fPort, "ph": ph},
    )

    if ph is None:
        # Status / no-measurement frame — accept but persist nothing.
        body = ChirpStackUplinkResponse(accepted=True, devEui=dev_eui, channels=0)
        return Response(body.model_dump(), status=202)

    zone = _lora_zone()
    PhSoil.objects.create(
        user=zone.user,
        zone=zone,
        value=ph,
        timestamp=timezone.now(),
    )
    body = ChirpStackUplinkResponse(accepted=True, devEui=dev_eui, channels=1)
    return Response(body.model_dump(), status=201)
