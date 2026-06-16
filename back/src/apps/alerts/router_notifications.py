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
    """Body for the one-shot zone-config confirmation email.

    Untyped extras (``zoneId``, ``zoneName``, …) are accepted but ignored —
    the legacy DRF view did the same.
    """

    channels: _Channels | None = None
    contactEmail: str | None = None
    subject: str | None = None
    message: str | None = None
    zoneId: int | None = None


@router.post(
    "/zone-outbound",
    auth=JwtAuth(),
    summary="One-shot zone-config confirmation email",
)
def zone_notification_outbound(request, payload: ZoneNotificationOutboundIn):
    channels = (
        payload.channels.model_dump(exclude_unset=True) if payload.channels else {}
    )
    if not channels.get("email"):
        return Response({"status": "noop"}, status=202)

    recipient = (payload.contactEmail or "").strip() or (
        getattr(request.auth, "email", "") or ""
    ).strip()
    if not recipient:
        return Response({"detail": "no email address on user"}, status=400)

    subject = (payload.subject or "").strip() or "Agrilogy — notification"
    message = (payload.message or "").strip() or (
        "La configuration de notification de zone a été enregistrée."
    )

    # Hand off to Celery so the request never blocks on a slow/unreachable
    # SMTP server (a dead mail host would otherwise hang the request until
    # nginx 504s). Delivery + error logging happen in the worker.
    from agriapi.tasks import send_zone_outbound_email

    send_zone_outbound_email.delay(
        recipient=recipient, subject=subject, message=message
    )
    log.info(
        "zone-notification-outbound: queued for %s zone=%s",
        recipient,
        payload.zoneId,
    )
    return Response({"status": "queued"}, status=202)
