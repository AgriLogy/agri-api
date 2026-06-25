"""Twilio SMS + WhatsApp sender — dependency-light (stdlib urllib, no SDK).

Used by the notification-panel zone-outbound flow to deliver a one-shot
message over SMS and/or WhatsApp in addition to email.

Credentials come from the environment:
  - ``TWILIO_ACCOUNT_SID``
  - ``TWILIO_AUTH_TOKEN``
  - ``TWILIO_SMS_FROM``       (e.g. ``+14155551234`` — a Twilio number)
  - ``TWILIO_WHATSAPP_FROM``  (e.g. ``whatsapp:+14155238886`` — sandbox or sender)

Read straight from ``os.getenv`` (not Django settings) so this stays
self-contained and free of settings wiring. Each ``send_*`` returns True on a
2xx Twilio response, False otherwise (never raises into the caller).
"""

from __future__ import annotations

import base64
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

TWILIO_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _creds() -> tuple[str, str]:
    return (
        (os.getenv("TWILIO_ACCOUNT_SID", "") or "").strip(),
        (os.getenv("TWILIO_AUTH_TOKEN", "") or "").strip(),
    )


def _to_e164(addr: str) -> str:
    """Best-effort normalise a phone number to ``+<digits>`` E.164."""
    a = (addr or "").strip()
    if not a:
        return ""
    if a.startswith("whatsapp:"):
        a = a[len("whatsapp:") :].strip()
    if a.startswith("+"):
        return "+" + "".join(ch for ch in a[1:] if ch.isdigit())
    return "+" + "".join(ch for ch in a if ch.isdigit())


def sms_configured() -> bool:
    sid, token = _creds()
    return bool(sid and token and (os.getenv("TWILIO_SMS_FROM", "") or "").strip())


def whatsapp_configured() -> bool:
    sid, token = _creds()
    return bool(sid and token and (os.getenv("TWILIO_WHATSAPP_FROM", "") or "").strip())


def _post_message(*, sender: str, to: str, body: str, channel: str) -> bool:
    sid, token = _creds()
    if not (sid and token and sender):
        logger.warning("%s not configured (TWILIO_* missing) — skipping", channel)
        return False
    if not to:
        logger.info("%s: no usable recipient — skipping", channel)
        return False

    data = urllib.parse.urlencode({"From": sender, "To": to, "Body": body}).encode(
        "utf-8"
    )
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(
        TWILIO_MESSAGES_URL.format(sid=sid),
        data=data,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        logger.error("Twilio %s send failed (HTTP %s): %s", channel, exc.code, detail)
        return False
    except Exception:
        logger.exception("Twilio %s send failed", channel)
        return False


def send_sms(to_phone: str, body: str) -> bool:
    """Send one SMS via Twilio. Returns True on a 2xx response."""
    sender = (os.getenv("TWILIO_SMS_FROM", "") or "").strip()
    return _post_message(sender=sender, to=_to_e164(to_phone), body=body, channel="SMS")


def send_whatsapp(to_phone: str, body: str) -> bool:
    """Send one WhatsApp message via Twilio. Returns True on a 2xx response."""
    sender = (os.getenv("TWILIO_WHATSAPP_FROM", "") or "").strip()
    if sender and not sender.startswith("whatsapp:"):
        sender = f"whatsapp:{sender}"
    e164 = _to_e164(to_phone)
    to = f"whatsapp:{e164}" if e164 else ""
    return _post_message(sender=sender, to=to, body=body, channel="WhatsApp")
