"""Notifications endpoints (django-ninja).

Migrated from ``analytics.views``:
  * GET  /api/notifications-and-alerts/   — paged feed of stored notifications
  * POST /api/zone-notification-outbound/ — one-shot zone-config email
"""

from __future__ import annotations

import logging
from typing import Any

from ninja import Router, Schema
from ninja.responses import Response

from agriapi.api.auth import JwtAuth
from analytics.models import Notification

router = Router()
log = logging.getLogger(__name__)


def _serialize_notification(n: Notification) -> dict[str, Any]:
    return {
        "id": n.id,
        "is_read": False,
        "read_at": None,
        "zone_name": None,
        "_source": "server",
        "notification": {
            "yesterday_temperature": str(n.yesterday_temperature),
            "today_temperature": str(n.today_temperature),
            "yesterday_humidity": str(n.yesterday_humidity),
            "today_humidity": str(n.today_humidity),
            "ET0": str(n.ET0),
            "soil_humidity": str(n.soil_humidity),
            "soil_temperature": str(n.soil_temperature),
            "soil_ph": str(n.soil_ph),
            "perfect_irrigation_period": n.perfect_irrigation_period,
            "last_irrigation_date": (
                n.last_irrigation_date.isoformat() if n.last_irrigation_date else None
            ),
            "last_start_irrigation_hour": (
                n.last_start_irrigation_hour.isoformat()
                if n.last_start_irrigation_hour
                else None
            ),
            "last_finish_irrigation_hour": (
                n.last_finish_irrigation_hour.isoformat()
                if n.last_finish_irrigation_hour
                else None
            ),
            "used_water_irrigation": str(n.used_water_irrigation),
            "notification_date": n.notification_date.isoformat(),
        },
    }


@router.get(
    "",
    auth=JwtAuth(),
    summary="Recent server-stored notifications for the caller",
)
def notifications_and_alerts(request):
    rows = Notification.objects.filter(user=request.auth).order_by(
        "-notification_date"
    )[:200]
    return {"notifications": [_serialize_notification(r) for r in rows]}


class _Channels(Schema):
    email: bool | None = None
    sms: bool | None = None
    whatsapp: bool | None = None


class ZoneNotificationOutboundIn(Schema):
    """Body for the one-shot zone notification (email / SMS / WhatsApp).

    Untyped extras (``zoneId``, ``zoneName``, …) are accepted but ignored —
    the legacy DRF view did the same.
    """

    channels: _Channels | None = None
    contactEmail: str | None = None
    contactPhone: str | None = None
    subject: str | None = None
    message: str | None = None
    zoneId: int | None = None


@router.post(
    "/zone-outbound",
    auth=JwtAuth(),
    summary="One-shot zone notification over email / SMS / WhatsApp",
)
def zone_notification_outbound(request, payload: ZoneNotificationOutboundIn):
    channels = (
        payload.channels.model_dump(exclude_unset=True) if payload.channels else {}
    )
    email_on = bool(channels.get("email"))
    sms_on = bool(channels.get("sms"))
    whatsapp_on = bool(channels.get("whatsapp"))
    if not (email_on or sms_on or whatsapp_on):
        return Response({"status": "noop"}, status=202)

    subject = (payload.subject or "").strip() or "Agrilogy — notification"
    message = (payload.message or "").strip() or (
        "La configuration de notification de zone a été enregistrée."
    )

    # Hand off to Celery so the request never blocks on a slow/unreachable
    # SMTP server or Twilio endpoint. Delivery + error logging happen in the
    # worker.
    from agriapi.tasks import (
        send_zone_outbound_email,
        send_zone_outbound_sms,
        send_zone_outbound_whatsapp,
    )

    queued: list[str] = []

    if email_on:
        recipient = (payload.contactEmail or "").strip() or (
            getattr(request.auth, "email", "") or ""
        ).strip()
        if recipient:
            send_zone_outbound_email.delay(
                recipient=recipient, subject=subject, message=message
            )
            queued.append("email")

    if sms_on or whatsapp_on:
        phone = (payload.contactPhone or "").strip() or (
            getattr(request.auth, "phone_number", "") or ""
        ).strip()
        if phone:
            if sms_on:
                send_zone_outbound_sms.delay(to_phone=phone, body=message)
                queued.append("sms")
            if whatsapp_on:
                send_zone_outbound_whatsapp.delay(to_phone=phone, body=message)
                queued.append("whatsapp")

    if not queued:
        return Response(
            {"detail": "no usable recipient for the selected channels"},
            status=400,
        )

    log.info(
        "zone-notification-outbound: queued %s zone=%s",
        ",".join(queued),
        payload.zoneId,
    )
    return Response({"status": "queued", "channels": queued}, status=202)
