"""fastapp /notifications — server-stored notification feed + zone outbound.

Strangler port of ``apps/alerts/router_notifications.py`` (django-ninja).
Byte-parity with the Django version:

* ``GET  /notifications`` — the caller's 200 most-recent stored
  notifications, newest first, same nested ``notification`` payload.
* ``POST /notifications/zone-outbound`` — one-shot zone notification over
  email / SMS / WhatsApp; same ``noop`` / ``queued`` / 400 contract, and the
  SAME Celery tasks (by name + kwargs) the Django route enqueued via
  ``<task>.delay(...)``.

Data access is SQLAlchemy via agri-core (no Django ORM); enqueue goes through
``fastapp.celery`` so the request never blocks on SMTP / Twilio.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agri.core.database import session_scope
from agri.db.analytics import AnalyticsNotification
from agri.db.users import CustomUserCustomuser
from sqlalchemy import select

from fastapp import celery
from fastapp.auth import AuthedUser, get_current_user
from fastapp.json import DjangoStyleJSONResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["notifications"])


def _serialize_notification(n: AnalyticsNotification) -> dict[str, Any]:
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
    "/notifications",
    summary="Recent server-stored notifications for the caller",
)
def notifications_and_alerts(user: AuthedUser = Depends(get_current_user)):
    with session_scope() as session:
        rows = session.scalars(
            select(AnalyticsNotification)
            .where(AnalyticsNotification.user_id == user.id)
            .order_by(AnalyticsNotification.notification_date.desc())
            .limit(200)
        ).all()
        return {"notifications": [_serialize_notification(r) for r in rows]}


class _Channels(BaseModel):
    email: bool | None = None
    sms: bool | None = None
    whatsapp: bool | None = None


class ZoneNotificationOutboundIn(BaseModel):
    """Body for the one-shot zone notification (email / SMS / WhatsApp).

    Untyped extras (``zoneName``, …) are accepted but ignored — pydantic's
    default ``extra='ignore'`` matches the ninja Schema's behaviour.
    """

    channels: _Channels | None = None
    contactEmail: str | None = None
    contactPhone: str | None = None
    subject: str | None = None
    message: str | None = None
    zoneId: int | None = None


def _user_phone(user_id: int) -> str:
    """The caller's stored phone number (fallback recipient for SMS/WhatsApp)."""
    with session_scope() as session:
        row = session.get(CustomUserCustomuser, user_id)
        return (getattr(row, "phone_number", None) or "") if row else ""


@router.post(
    "/notifications/zone-outbound",
    summary="One-shot zone notification over email / SMS / WhatsApp",
)
def zone_notification_outbound(
    payload: ZoneNotificationOutboundIn,
    user: AuthedUser = Depends(get_current_user),
):
    channels = (
        payload.channels.model_dump(exclude_unset=True) if payload.channels else {}
    )
    email_on = bool(channels.get("email"))
    sms_on = bool(channels.get("sms"))
    whatsapp_on = bool(channels.get("whatsapp"))
    if not (email_on or sms_on or whatsapp_on):
        return DjangoStyleJSONResponse({"status": "noop"}, status_code=202)

    subject = (payload.subject or "").strip() or "Agrilogy — notification"
    message = (payload.message or "").strip() or (
        "La configuration de notification de zone a été enregistrée."
    )

    # Hand off to Celery so the request never blocks on a slow/unreachable
    # SMTP server or Twilio endpoint. Delivery + error logging happen in the
    # worker (same task, by name + kwargs, the Django route enqueued).
    queued: list[str] = []

    if email_on:
        recipient = (payload.contactEmail or "").strip() or (user.email or "").strip()
        if recipient:
            celery.send_task(
                "agriapi.tasks.send_zone_outbound_email",
                recipient=recipient,
                subject=subject,
                message=message,
            )
            queued.append("email")

    if sms_on or whatsapp_on:
        phone = (payload.contactPhone or "").strip() or _user_phone(user.id).strip()
        if phone:
            if sms_on:
                celery.send_task(
                    "agriapi.tasks.send_zone_outbound_sms",
                    to_phone=phone,
                    body=message,
                )
                queued.append("sms")
            if whatsapp_on:
                celery.send_task(
                    "agriapi.tasks.send_zone_outbound_whatsapp",
                    to_phone=phone,
                    body=message,
                )
                queued.append("whatsapp")

    if not queued:
        raise HTTPException(
            status_code=400,
            detail="no usable recipient for the selected channels",
        )

    log.info(
        "zone-notification-outbound: queued %s zone=%s",
        ",".join(queued),
        payload.zoneId,
    )
    return DjangoStyleJSONResponse(
        {"status": "queued", "channels": queued}, status_code=202
    )
