"""Minimal Resend HTTPS email client for fastapp (stdlib urllib only).

The DigitalOcean droplet blocks outbound SMTP, so mail goes over Resend's
HTTPS API — the same path the Django ``ResendEmailBackend`` uses. This is a
framework-free extract of that POST (no django.core.mail); a later phase
lifts it into ``agri.core.notifications`` so Celery + fastapp share one
sender. Best-effort by contract: returns False on any failure and never
raises (callers treat email as a non-blocking side effect).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def send_email(
    *,
    api_key: str,
    from_email: str,
    to: list[str],
    subject: str,
    text: str,
    html: str | None = None,
    timeout: int = 10,
) -> bool:
    """POST one message to Resend. Returns True on 2xx, False otherwise.

    Never raises — email is a best-effort side effect. A missing api_key /
    empty recipient list is a logged no-op (matches the Django backend's
    fail_silently path)."""
    if not api_key:
        logger.info(
            "notify.email.skipped",
            extra={
                "event": "notify.email.skipped",
                "provider": "resend",
                "reason": "no_api_key",
                "recipient_count": len(to),
            },
        )
        return False
    if not to:
        return False

    payload: dict[str, object] = {
        "from": from_email,
        "to": to,
        "subject": subject,
        "text": text,
    }
    if html:
        payload["html"] = html

    request = urllib.request.Request(
        RESEND_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend sits behind Cloudflare, which 403s the default
            # ``Python-urllib/x.y`` agent (error 1010). Explicit UA required.
            "User-Agent": "agri-api-fastapp/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = resp.status
            resp.read()
        logger.info(
            "notify.email.sent",
            extra={
                "event": "notify.email.sent",
                "provider": "resend",
                "status_code": status,
                "recipient_count": len(to),
            },
        )
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        # HTTP 429 = Resend daily quota exhausted — the known prod blocker for
        # notifications. Surfaced as its own boolean so a Loki alert can fire on
        # `quota_exceeded=true` the moment it starts, not hours later.
        logger.error(
            "notify.email.failed",
            extra={
                "event": "notify.email.failed",
                "provider": "resend",
                "status_code": exc.code,
                "quota_exceeded": exc.code == 429,
                "recipient_count": len(to),
                "error": body[:500],
            },
        )
        return False
    except Exception as exc:
        logger.error(
            "notify.email.error",
            extra={
                "event": "notify.email.error",
                "provider": "resend",
                "recipient_count": len(to),
                "error": str(exc)[:500],
            },
            exc_info=True,
        )
        return False
