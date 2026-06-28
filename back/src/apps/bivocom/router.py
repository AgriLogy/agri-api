"""Bivocom ingest router (django-ninja).

``POST /api/v1/bivocom/uplink`` — mounted from ``agriapi.api`` so the
URL stays identical to the legacy DRF route.

Auth: ``auth=None`` because the gateway calls in via a shared secret
header (TODO: enforce in a follow-up).

The uplink is routed entirely by DATA: the gateway's ``device_id`` resolves
to a registered :class:`~apps.irrigation.models.Device`, and that device's
:class:`~apps.irrigation.models.DeviceSensor` rows map each incoming wire tag
to a ``sensor_key`` + farm zone. An admin can therefore onboard a new router
and its sensors entirely from the back-office — no code change per device.
"""

from __future__ import annotations

import logging

from django.utils import timezone
from ninja import Router
from ninja.responses import Response

from apps.bivocom.schemas import BivocomUplink, BivocomUplinkResponse

router = Router()
log = logging.getLogger("bivocom")


@router.post(
    "",
    response={202: BivocomUplinkResponse, 404: dict, 422: dict},
    auth=None,
    summary="Bivocom gateway uplink webhook (POST /ingest/bivocom)",
)
def bivocom_uplink(request, payload: BivocomUplink):
    from agri.core.alerts import SENSOR_KEY_REGISTRY

    from apps.alerts.engine import dispatch_alerts_for_reading, get_sensor_model
    from apps.irrigation.models import Device, DeviceSensor

    device = (
        Device.objects.filter(serial=payload.device_id)
        .select_related("user", "zone")
        .first()
    )
    if device is None:
        log.warning("bivocom.uplink unknown device_id=%s", payload.device_id)
        return Response(
            {"detail": f"No device registered for device_id '{payload.device_id}'."},
            status=404,
        )
    if not device.is_active:
        return Response({"detail": "device is inactive."}, status=422)
    if device.user_id is None:
        return Response(
            {"detail": "device has no owner; assign one before ingest."}, status=422
        )

    # tag -> DeviceSensor (active mappings only). Each says "this wire tag is a
    # sensor_key feeding zone Z (or the device's own zone)".
    sensors = {
        s.tag_name: s
        for s in DeviceSensor.objects.filter(
            device=device, is_active=True
        ).select_related("zone")
    }

    ts = payload.timestamp or timezone.now()
    accepted = 0
    skipped: list[str] = []

    for tag, value in payload.tags.items():
        sensor = sensors.get(tag)
        if sensor is None or sensor.sensor_key not in SENSOR_KEY_REGISTRY:
            skipped.append(tag)
            continue
        zone = sensor.zone or device.zone
        if zone is None:
            # No zone to attribute the reading to — nothing sensible to store.
            skipped.append(tag)
            continue
        try:
            model_cls = get_sensor_model(sensor.sensor_key)
            model_cls.objects.create(
                user=device.user, zone=zone, value=value, timestamp=ts
            )
            accepted += 1
        except Exception:
            log.exception(
                "bivocom persist failed tag=%s sensor_key=%s", tag, sensor.sensor_key
            )
            skipped.append(tag)
            continue
        # Alert dispatch must never abort the ingest loop — the reading is
        # already stored; a downstream alerts bug shouldn't drop later tags.
        try:
            dispatch_alerts_for_reading(
                sensor_key=sensor.sensor_key,
                zone=zone,
                user=device.user,
                value=value,
                timestamp=ts,
            )
        except Exception:
            log.exception("bivocom alert dispatch failed tag=%s", tag)

    log.info(
        "bivocom.uplink device=%s accepted=%d skipped=%s",
        payload.device_id,
        accepted,
        skipped,
    )

    body = BivocomUplinkResponse(
        accepted=True,
        device_id=payload.device_id,
        tag_count=accepted,
    )
    return Response(body.model_dump(), status=202)
