"""ChirpStack webhook view — Phase 5b placeholder.

Validates the v4 uplink event shape via pydantic, logs it, returns 202.
Phase 6.5 wires in ``agri.core.devices.lorawan.chirpstack.ChirpStackAdapter``
for the real normalization → ``SensorReading[]`` → persistence pipeline.
"""
from __future__ import annotations

import logging

from pydantic import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.lorawan.chirpstack.schemas import ChirpStackUplink, ChirpStackUplinkResponse

log = logging.getLogger("lorawan.chirpstack")


class ChirpStackUplinkView(APIView):
    """``POST /api/v1/lorawan/chirpstack/uplink``

    Configure in ChirpStack as an HTTP integration:
      Endpoint URL: https://<host>/api/v1/lorawan/chirpstack/uplink
      Headers:      X-Agri-Token: <shared-secret>  (TODO: enforce in Phase 5c+)
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request: Request) -> Response:
        try:
            payload = ChirpStackUplink.model_validate(request.data)
        except ValidationError as exc:
            log.warning("chirpstack.uplink.invalid", extra={"errors": exc.errors()})
            return Response(
                {
                    "error": {
                        "code": "validation_error",
                        "message": "invalid ChirpStack uplink event",
                        "details": exc.errors(),
                    }
                },
                status=400,
            )

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
        # TODO(Phase 6.5): hand off to ChirpStackAdapter.normalize() and
        # persist the resulting SensorReading[] via the ingest controller.

        body = ChirpStackUplinkResponse(
            accepted=True,
            devEui=payload.deviceInfo.devEui,
            channels=len(payload.object),
        )
        return Response(body.model_dump(), status=202)
