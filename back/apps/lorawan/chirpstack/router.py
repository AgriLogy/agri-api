"""ChirpStack webhook router (django-ninja).

``POST /api/v1/lorawan/chirpstack/uplink`` — mounted from
``agriBack.api`` so the URL stays identical to the legacy DRF route.

Auth: ``auth=None`` because the gateway calls in via a shared secret
header (TODO: enforce in a follow-up); the v2 NinjaAPI's default JWT
auth is opted out per-route.
"""
from __future__ import annotations

import logging

from ninja import Router, Schema
from ninja.responses import Response

from apps.lorawan.chirpstack.schemas import ChirpStackUplink, ChirpStackUplinkResponse

_ = Schema  # placate import linter; pydantic is provided by ninja

router = Router()
log = logging.getLogger("lorawan.chirpstack")


@router.post(
    "",
    response={202: ChirpStackUplinkResponse, 400: dict},
    auth=None,
    summary="ChirpStack v4 uplink webhook (POST /ingest/lorawan/chirpstack)",
)
def chirpstack_uplink(request, payload: ChirpStackUplink):
    log.info(
        "chirpstack.uplink",
        extra={
            "dev_eui": payload.deviceInfo.devEui,
            "device_name": payload.deviceInfo.deviceName,
            "channels": len(payload.object),
            "rssi": payload.rxInfo[0].rssi if payload.rxInfo else None,
            "snr": payload.rxInfo[0].snr if payload.rxInfo else None,
        },
    )
    # TODO(Phase 6.5): hand off to ChirpStackAdapter.normalize() and persist
    # the resulting SensorReading[] via the ingest controller.
    body = ChirpStackUplinkResponse(
        accepted=True,
        devEui=payload.deviceInfo.devEui,
        channels=len(payload.object),
    )
    return Response(body.model_dump(), status=202)
