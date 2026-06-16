"""Custom Django email backend that delivers via the Resend HTTP API.

Why this exists: DigitalOcean blocks outbound SMTP (ports 25/465/587) on our
droplet, so the stock ``smtp`` backend hangs on connect and the request 504s —
no mail ever leaves. Resend's REST API runs over HTTPS/443, which *is* open.

This backend is a drop-in: point ``EMAIL_BACKEND`` here and every
``send_mail()`` / ``EmailMessage`` call (periodic notifications, alert emails,
the notification-panel custom send) routes through Resend unchanged.

Stdlib-only (``urllib``), mirroring the Twilio integration's no-new-deps
approach. Honours ``settings.EMAIL_TIMEOUT`` and the standard
``fail_silently`` contract.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


class ResendEmailBackend(BaseEmailBackend):
    """Send Django emails through the Resend HTTPS API."""

    def __init__(self, *, fail_silently: bool = False, **kwargs) -> None:
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.api_key = (
            getattr(settings, "RESEND_API_KEY", "")
            or os.getenv("RESEND_API_KEY", "")
            or ""
        ).strip()
        self.timeout = getattr(settings, "EMAIL_TIMEOUT", 10) or 10

    def send_messages(self, email_messages) -> int:
        if not email_messages:
            return 0
        if not self.api_key:
            if not self.fail_silently:
                raise RuntimeError(
                    "RESEND_API_KEY is not configured; cannot send email."
                )
            logger.error("ResendEmailBackend: RESEND_API_KEY missing — dropping mail")
            return 0

        sent = 0
        for message in email_messages:
            if self._send_one(message):
                sent += 1
        return sent

    def _send_one(self, message) -> bool:
        recipients = message.recipients()
        if not recipients:
            return False

        payload: dict[str, object] = {
            "from": message.from_email,
            "to": list(message.to) or recipients,
            "subject": message.subject,
            "text": message.body,
        }
        if message.cc:
            payload["cc"] = list(message.cc)
        if message.bcc:
            payload["bcc"] = list(message.bcc)
        if getattr(message, "reply_to", None):
            payload["reply_to"] = list(message.reply_to)
        # Prefer an HTML alternative when the caller attached one.
        for content, mimetype in getattr(message, "alternatives", None) or []:
            if mimetype == "text/html":
                payload["html"] = content
                break

        request = urllib.request.Request(
            RESEND_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # Resend's API sits behind Cloudflare, which 403s the default
                # ``Python-urllib/x.y`` agent (error 1010). Send an explicit UA.
                "User-Agent": "agri-api-resend-backend/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            logger.error("Resend send failed (HTTP %s): %s", exc.code, body)
            if not self.fail_silently:
                raise
            return False
        except Exception:
            logger.exception("Resend send failed")
            if not self.fail_silently:
                raise
            return False
