"""Twilio WhatsApp sender — dependency-light (stdlib urllib, no SDK).

Credentials come from settings/env: ``TWILIO_ACCOUNT_SID``,
``TWILIO_AUTH_TOKEN``, ``TWILIO_WHATSAPP_FROM`` (e.g. ``whatsapp:+14155238886``
for the sandbox). If any is missing the send is a logged no-op, so the rest of
the alert pipeline keeps working until the credentials are set on the droplet.
"""

from __future__ import annotations

import base64
import logging
import os
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

TWILIO_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _cfg(name: str) -> str:
    return (getattr(settings, name, "") or os.environ.get(name, "") or "").strip()


def _as_whatsapp(addr: str) -> str:
    """Normalise to Twilio's ``whatsapp:+<E164>`` form."""
    addr = addr.strip()
    if not addr:
        return ""
    if addr.startswith("whatsapp:"):
        return addr
    if not addr.startswith("+"):
        addr = "+" + addr.lstrip("0")
    return f"whatsapp:{addr}"


def whatsapp_configured() -> bool:
    return bool(
        _cfg("TWILIO_ACCOUNT_SID")
        and _cfg("TWILIO_AUTH_TOKEN")
        and _cfg("TWILIO_WHATSAPP_FROM")
    )


def send_whatsapp(to_phone: str, body: str) -> bool:
    """Send one WhatsApp message via Twilio. Returns True on a 2xx response."""
    sid, token, sender = (
        _cfg("TWILIO_ACCOUNT_SID"),
        _cfg("TWILIO_AUTH_TOKEN"),
        _cfg("TWILIO_WHATSAPP_FROM"),
    )
    if not (sid and token and sender):
        logger.warning(
            "WhatsApp not configured (TWILIO_* missing) — skipping send to %s", to_phone
        )
        return False

    to = _as_whatsapp(to_phone or "")
    if not to or to == "whatsapp:+":
        logger.info("WhatsApp: no usable recipient phone — skipping")
        return False

    data = urllib.parse.urlencode(
        {"From": _as_whatsapp(sender), "To": to, "Body": body}
    ).encode("utf-8")
    req = urllib.request.Request(
        TWILIO_MESSAGES_URL.format(sid=sid), data=data, method="POST"
    )
    req.add_header(
        "Authorization",
        "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode(),
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            if ok:
                logger.info("WhatsApp sent to %s", to)
            return ok
    except Exception:
        logger.exception("Twilio WhatsApp send failed to %s", to)
        return False
