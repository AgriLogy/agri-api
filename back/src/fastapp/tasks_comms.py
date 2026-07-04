"""fastapp comms task bodies (F8) — the on-demand notification deliverers.

Django-free ports of the seven communication tasks in ``agriapi/tasks.py``:
``send_alert_email`` / ``send_alert_digest_email`` / ``send_alert_whatsapp`` /
``send_alert_sms`` and ``send_zone_outbound_{email,sms,whatsapp}``. They are
plain functions here; the native Celery app (F10) wraps them as tasks under the
SAME names (``agriapi.tasks.<name>``) so the wire contract with the enqueuers
(fastapp ``ingest.py`` + ``routers/notifications.py``) is unchanged.

DB access is via the agri-core SQLAlchemy session; sensor labels/units come from
``agri.core.alerts.SENSOR_KEY_REGISTRY``; email via :mod:`fastapp.email`
(Resend) and SMS/WhatsApp via :mod:`fastapp.sms` (Twilio). Every function is
defensive (reload + skip inactive/deleted, bail on missing recipient) and never
raises, so Celery won't retry-and-dupe — byte-for-byte the Django semantics.
"""

from __future__ import annotations

import datetime
import logging

from agri.core.alerts import SENSOR_KEY_REGISTRY
from agri.core.database import session_scope
from agri.db.analytics import AnalyticsAlert
from agri.db.audit import AnalyticsNotificationdeliverylog
from agri.db.users import CustomUserCustomuser
from fastapp import email, sms
from fastapp.settings import get_settings

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _record_delivery(
    session,
    *,
    channel: str,
    kind: str = "",
    recipient: str = "",
    user_id: int | None = None,
    status: str = "sent",
    error: str = "",
) -> None:
    """Best-effort delivery-log row (mirrors agriapi.delivery_log). Never raises."""
    try:
        session.add(
            AnalyticsNotificationdeliverylog(
                channel=(channel or "")[:16],
                kind=(kind or "")[:24],
                recipient=(recipient or "")[:255],
                user_id=user_id,
                status=(status or "")[:12],
                error=(error or "")[:2000],
                created_at=_utcnow(),
            )
        )
        session.flush()
    except Exception as exc:  # pragma: no cover - fail-soft
        logger.warning("delivery record failed for %s/%s: %s", channel, kind, exc)


def _send_email(*, to: str, subject: str, body: str) -> bool:
    s = get_settings()
    return email.send_email(
        api_key=s.resend_api_key,
        from_email=s.default_from_email,
        to=[to],
        subject=subject,
        text=body,
    )


def _spec(sensor_key: str) -> tuple[str, str]:
    spec = SENSOR_KEY_REGISTRY.get(sensor_key, {})
    return (spec.get("label") or sensor_key), (spec.get("unit") or "")


# ---------------------------------------------------------------------------
# alert deliverers (enqueued by dispatch_alerts_for_reading)
# ---------------------------------------------------------------------------
def send_alert_email(*, alert_id: int, value: float, timestamp_iso: str) -> dict:
    with session_scope(commit=True) as session:
        alert = session.get(AnalyticsAlert, alert_id)
        if alert is None:
            logger.info("send_alert_email: alert %s gone — skipping", alert_id)
            return {"sent": 0, "reason": "alert_missing"}
        if not alert.is_active:
            return {"sent": 0, "reason": "alert_inactive"}
        user = (
            session.get(CustomUserCustomuser, alert.user_id) if alert.user_id else None
        )
        recipient = (getattr(user, "email", "") or "").strip() if user else ""
        if not recipient:
            return {"sent": 0, "reason": "no_recipient"}

        label, unit = _spec(alert.sensor_key)
        zone = _zone_name(session, alert.zone_id) if alert.zone_id else None
        zone_label = zone or "votre compte"
        subject = f"Alerte — {alert.name}"
        body = (
            f"Bonjour {getattr(user, 'firstname', '') or user.username},\n\n"
            f"L'alerte « {alert.name} » sur {zone_label} s'est déclenchée.\n\n"
            f"Capteur     : {label} ({alert.sensor_key})\n"
            f"Valeur      : {value} {unit}\n"
            f"Seuil       : {alert.condition} {alert.condition_nbr}\n"
            f"Horodatage  : {timestamp_iso}\n\n"
            f"Vous pouvez ajuster ou désactiver cette alerte depuis votre tableau "
            f"de bord.\n"
        )
        ok = _send_email(to=recipient, subject=subject, body=body)
        if not ok:
            _record_delivery(
                session,
                channel="email",
                kind="alert",
                recipient=recipient,
                user_id=user.id,
                status="failed",
                error="send_error",
            )
            return {"sent": 0, "reason": "smtp_error"}
        _record_delivery(
            session,
            channel="email",
            kind="alert",
            recipient=recipient,
            user_id=user.id,
            status="sent",
        )
        return {"sent": 1, "alert_id": alert_id}


def send_alert_digest_email(
    *, alert_ids: list[int], value: float, timestamp_iso: str
) -> dict:
    with session_scope(commit=True) as session:
        alerts = [
            a
            for a in (session.get(AnalyticsAlert, aid) for aid in alert_ids)
            if a is not None and a.is_active
        ]
        if not alerts:
            return {"sent": 0, "reason": "no_active_alerts"}
        user = (
            session.get(CustomUserCustomuser, alerts[0].user_id)
            if alerts[0].user_id
            else None
        )
        recipient = (getattr(user, "email", "") or "").strip() if user else ""
        if not recipient:
            return {"sent": 0, "reason": "no_recipient"}

        lines = []
        for a in alerts:
            label, unit = _spec(a.sensor_key)
            zone = _zone_name(session, a.zone_id) if a.zone_id else None
            zone_label = zone or "votre compte"
            lines.append(
                f"• « {a.name} » ({zone_label}) — {label}: {value} {unit} "
                f"(seuil {a.condition} {a.condition_nbr})"
            )
        subject = f"Alertes déclenchées ({len(alerts)})"
        body = (
            f"Bonjour {getattr(user, 'firstname', '') or user.username},\n\n"
            f"Plusieurs alertes se sont déclenchées sur la même lecture :\n\n"
            + "\n".join(lines)
            + f"\n\nHorodatage  : {timestamp_iso}\n"
        )
        ok = _send_email(to=recipient, subject=subject, body=body)
        status = "sent" if ok else "failed"
        _record_delivery(
            session,
            channel="email",
            kind="alert",
            recipient=recipient,
            user_id=user.id,
            status=status,
            error="" if ok else "send_error",
        )
        return {"sent": 1 if ok else 0, "alert_ids": [a.id for a in alerts]}


def send_alert_whatsapp(*, alert_id: int, value: float, timestamp_iso: str) -> dict:
    with session_scope() as session:
        alert = session.get(AnalyticsAlert, alert_id)
        if alert is None:
            return {"sent": 0, "reason": "alert_missing"}
        if not alert.is_active:
            return {"sent": 0, "reason": "alert_inactive"}
        user = (
            session.get(CustomUserCustomuser, alert.user_id) if alert.user_id else None
        )
        phone = (getattr(user, "phone_number", "") or "").strip() if user else ""
        if not phone:
            return {"sent": 0, "reason": "no_phone"}
        label, unit = _spec(alert.sensor_key)
        zone = _zone_name(session, alert.zone_id) if alert.zone_id else None
        zone_label = zone or "votre compte"
        body = (
            f"🌱 Alerte « {alert.name} » ({zone_label})\n"
            f"{label}: {value} {unit} (seuil {alert.condition} {alert.condition_nbr})\n"
            f"{timestamp_iso}"
        )
    ok = sms.send_whatsapp(phone, body)
    return {"sent": 1 if ok else 0, "alert_id": alert_id}


def send_alert_sms(*, alert_id: int, value: float, timestamp_iso: str) -> dict:
    with session_scope() as session:
        alert = session.get(AnalyticsAlert, alert_id)
        if alert is None:
            return {"sent": 0, "reason": "alert_missing"}
        if not alert.is_active:
            return {"sent": 0, "reason": "alert_inactive"}
        user = (
            session.get(CustomUserCustomuser, alert.user_id) if alert.user_id else None
        )
        phone = (getattr(user, "phone_number", "") or "").strip() if user else ""
        if not phone:
            return {"sent": 0, "reason": "no_phone"}
        label, unit = _spec(alert.sensor_key)
        if alert.zone_id:
            zone_label = _zone_name(session, alert.zone_id) or "votre compte"
        elif alert.notification_zone_id:
            zone_label = (
                _notification_zone_name(session, alert.notification_zone_id)
                or "votre compte"
            )
        else:
            zone_label = "votre compte"
        body = (
            f"Alerte « {alert.name} » ({zone_label}) — "
            f"{label}: {value} {unit} (seuil {alert.condition} {alert.condition_nbr}) "
            f"{timestamp_iso}"
        )
    ok = sms.send_sms(phone, body)
    return {"sent": 1 if ok else 0, "alert_id": alert_id}


# ---------------------------------------------------------------------------
# zone-outbound deliverers (enqueued by the notifications router)
# ---------------------------------------------------------------------------
def send_zone_outbound_email(*, recipient: str, subject: str, message: str) -> dict:
    recipient = (recipient or "").strip()
    if not recipient:
        return {"sent": 0, "reason": "no_recipient"}
    ok = _send_email(to=recipient, subject=subject, body=message)
    with session_scope(commit=True) as session:
        _record_delivery(
            session,
            channel="email",
            kind="outbound",
            recipient=recipient,
            status="sent" if ok else "failed",
            error="" if ok else "send_error",
        )
    if not ok:
        return {"sent": 0, "reason": "send_error"}
    logger.info("send_zone_outbound_email: sent to %s", recipient)
    return {"sent": 1, "recipient": recipient}


def send_zone_outbound_sms(*, to_phone: str, body: str) -> dict:
    to_phone = (to_phone or "").strip()
    if not to_phone:
        return {"sent": 0, "reason": "no_recipient"}
    ok = sms.send_sms(to_phone, body)
    with session_scope(commit=True) as session:
        _record_delivery(
            session,
            channel="sms",
            kind="outbound",
            recipient=to_phone,
            status="sent" if ok else "failed",
        )
    if ok:
        logger.info("send_zone_outbound_sms: sent to %s", to_phone)
    return {"sent": 1 if ok else 0, "recipient": to_phone}


def send_zone_outbound_whatsapp(*, to_phone: str, body: str) -> dict:
    to_phone = (to_phone or "").strip()
    if not to_phone:
        return {"sent": 0, "reason": "no_recipient"}
    ok = sms.send_whatsapp(to_phone, body)
    with session_scope(commit=True) as session:
        _record_delivery(
            session,
            channel="whatsapp",
            kind="outbound",
            recipient=to_phone,
            status="sent" if ok else "failed",
        )
    if ok:
        logger.info("send_zone_outbound_whatsapp: sent to %s", to_phone)
    return {"sent": 1 if ok else 0, "recipient": to_phone}


# ---------------------------------------------------------------------------
# small zone-name lookups (avoid importing the analytics zone models at top)
# ---------------------------------------------------------------------------
def _zone_name(session, zone_id: int) -> str | None:
    from agri.db.analytics import AnalyticsZone

    z = session.get(AnalyticsZone, zone_id)
    return z.name if z is not None else None


def _notification_zone_name(session, nz_id: int) -> str | None:
    from agri.db.analytics import AnalyticsNotificationzone

    z = session.get(AnalyticsNotificationzone, nz_id)
    return z.name if z is not None else None
