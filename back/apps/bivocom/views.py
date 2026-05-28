"""HTTP views for Bivocom ingest.

Phase 5a structural placeholder: validates the incoming payload via pydantic
and logs it. Phase 6.5 wires in ``agri.core.devices.BivocomAdapter`` to
normalize and persist readings.
"""
from __future__ import annotations

import logging

from pydantic import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.bivocom.schemas import BivocomUplink, BivocomUplinkResponse

log = logging.getLogger("bivocom")


class BivocomUplinkView(APIView):
    """``POST /api/v1/bivocom/uplink`` — accept a Bivocom gateway push.

    Auth: a shared-secret header will gate this in a follow-up; for now
    the endpoint is open so we can wire it up end-to-end with the
    physical gateway in the field.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request: Request) -> Response:
        try:
            payload = BivocomUplink.model_validate(request.data)
        except ValidationError as exc:
            log.warning("bivocom.uplink.invalid", extra={"errors": exc.errors()})
            return Response(
                {
                    "error": {
                        "code": "validation_error",
                        "message": "invalid Bivocom uplink payload",
                        "details": exc.errors(),
                    }
                },
                status=400,
            )

        log.info(
            "bivocom.uplink",
            extra={
                "device_id": payload.device_id,
                "timestamp": payload.timestamp.isoformat(),
                "tag_count": len(payload.tags),
                "rssi": payload.rssi,
            },
        )
        # TODO(Phase 6.5): hand off to agri.core.devices.BivocomAdapter
        # for normalization → list[SensorReading], then write through the
        # ingest controller to Postgres + enqueue computed_metrics refresh.

        body = BivocomUplinkResponse(
            accepted=True,
            device_id=payload.device_id,
            tag_count=len(payload.tags),
        )
        return Response(body.model_dump(), status=202)
