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
        logger.info("fastapp email: RESEND_API_KEY missing — dropping mail")
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
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        logger.error("fastapp Resend send failed (HTTP %s): %s", exc.code, body)
        return False
    except Exception:
        logger.exception("fastapp Resend send failed")
        return False
