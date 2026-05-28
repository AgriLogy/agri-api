"""Bivocom ingest router (django-ninja).

``POST /api/v1/bivocom/uplink`` — mounted from ``agriBack.api`` so the
URL stays identical to the legacy DRF route.

Auth: ``auth=None`` because the gateway calls in via a shared secret
header (TODO: enforce in a follow-up).
"""
from __future__ import annotations

import logging

from ninja import Router
from ninja.responses import Response

from apps.bivocom.schemas import BivocomUplink, BivocomUplinkResponse

router = Router()
log = logging.getLogger("bivocom")


@router.post(
    "/uplink",
    response={202: BivocomUplinkResponse, 422: dict},
    auth=None,
    summary="Bivocom gateway uplink webhook",
)
def bivocom_uplink(request, payload: BivocomUplink):
    log.info(
        "bivocom.uplink",
        extra={
            "device_id": payload.device_id,
            "timestamp": payload.timestamp.isoformat(),
            "tag_count": len(payload.tags),
            "rssi": payload.rssi,
        },
    )
    # TODO(Phase 6.5): hand off to agri.core.devices.BivocomAdapter for
    # normalization -> list[SensorReading], then write through the ingest
    # controller to Postgres + enqueue computed_metrics refresh.

    body = BivocomUplinkResponse(
        accepted=True,
        device_id=payload.device_id,
        tag_count=len(payload.tags),
    )
    return Response(body.model_dump(), status=202)
