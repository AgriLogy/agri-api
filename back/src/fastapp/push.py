"""Firebase Cloud Messaging (HTTP v1) client for mobile push (agri-api #37).

No Firebase SDK: a service-account JWT (RS256, PyJWT) is exchanged for an
OAuth2 access token at Google's token endpoint, which is cached until shortly
before it expires, then each message is one ``POST .../messages:send``.

Configured by ``FCM_CREDENTIALS`` (the service-account JSON, or a path to it).
Unset → :func:`is_configured` is False and callers skip sending.

:func:`send` never raises. It returns a :class:`PushResult` whose
``unregistered`` flag tells the caller to drop the token (FCM says the app was
uninstalled or the token is invalid).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from fastapp.settings import get_settings

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
TIMEOUT_S = 10.0

# FCM error codes meaning "this registration token will never work again".
# INVALID_ARGUMENT is deliberately NOT here: FCM also returns it for a
# malformed payload, and treating that as a dead token would wipe every
# device after one bad message.
_DEAD_TOKEN_CODES = {"UNREGISTERED", "SENDER_ID_MISMATCH"}

_lock = threading.Lock()
_cached: dict[str, Any] = {"token": None, "exp": 0.0, "key": None}


@dataclass(frozen=True)
class PushResult:
    ok: bool
    unregistered: bool = False
    error: str = ""


def load_service_account(raw: str | None = None) -> dict | None:
    """Parse ``FCM_CREDENTIALS`` (inline JSON or a file path). ``None`` when
    unset or unusable (logged)."""
    raw = (get_settings().fcm_credentials if raw is None else raw).strip()
    if not raw:
        return None
    try:
        if not raw.startswith("{"):
            with open(os.path.expanduser(raw), encoding="utf-8") as fh:
                raw = fh.read()
        info = json.loads(raw)
    except (OSError, ValueError) as exc:
        logger.error("push: FCM_CREDENTIALS unreadable: %s", exc)
        return None
    required = ("project_id", "client_email", "private_key")
    if not all(info.get(k) for k in required):
        logger.error("push: FCM_CREDENTIALS lacks %s", ", ".join(required))
        return None
    return info


def is_configured() -> bool:
    return load_service_account() is not None


def _access_token(info: dict, client: httpx.Client) -> str:
    """OAuth2 access token for ``info``'s service account, cached in-process
    until 60 s before expiry."""
    now = time.time()
    with _lock:
        if (
            _cached["token"]
            and _cached["key"] == info["client_email"]
            and now < _cached["exp"] - 60
        ):
            return _cached["token"]
    token_uri = info.get("token_uri") or DEFAULT_TOKEN_URI
    assertion = jwt.encode(
        {
            "iss": info["client_email"],
            "scope": SCOPE,
            "aud": token_uri,
            "iat": int(now),
            "exp": int(now) + 3600,
        },
        info["private_key"],
        algorithm="RS256",
        headers={"kid": info.get("private_key_id", "")}
        if info.get("private_key_id")
        else None,
    )
    resp = client.post(
        token_uri,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    body = resp.json()
    with _lock:
        _cached.update(
            token=body["access_token"],
            exp=now + float(body.get("expires_in", 3600)),
            key=info["client_email"],
        )
    return body["access_token"]


def reset_token_cache() -> None:
    with _lock:
        _cached.update(token=None, exp=0.0, key=None)


def build_message(token: str, *, title: str, body: str, data: dict[str, Any]) -> dict:
    """FCM v1 message: visible notification + string-only data payload, high
    priority on Android, default sound on iOS."""
    return {
        "message": {
            "token": token,
            "notification": {"title": title[:200], "body": body[:1000]},
            "data": {str(k): "" if v is None else str(v) for k, v in data.items()},
            "android": {"priority": "high"},
            "apns": {"payload": {"aps": {"sound": "default"}}},
        }
    }


def _error_code(resp: httpx.Response) -> str:
    """FCM puts the precise code in error.details[].errorCode; fall back to
    error.status."""
    try:
        err = resp.json().get("error", {})
    except ValueError:
        return ""
    for detail in err.get("details") or []:
        if detail.get("errorCode"):
            return detail["errorCode"]
    return err.get("status", "")


def send(
    token: str,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
) -> PushResult:
    """Send one notification to one registration token. Never raises."""
    info = load_service_account()
    if info is None:
        return PushResult(ok=False, error="not_configured")
    own = client is None
    client = client or httpx.Client()
    try:
        access = _access_token(info, client)
        resp = client.post(
            SEND_URL.format(project=info["project_id"]),
            json=build_message(token, title=title, body=body, data=data or {}),
            headers={"Authorization": f"Bearer {access}"},
            timeout=TIMEOUT_S,
        )
        if resp.status_code == 200:
            return PushResult(ok=True)
        code = _error_code(resp)
        if resp.status_code == 401:
            reset_token_cache()  # stale access token: next send re-auths
        dead = code in _DEAD_TOKEN_CODES or resp.status_code == 404
        return PushResult(
            ok=False, unregistered=dead, error=code or f"http_{resp.status_code}"
        )
    except Exception as exc:  # network, auth, bad key — never break the caller
        logger.warning("push: send failed: %s", exc)
        return PushResult(ok=False, error=type(exc).__name__)
    finally:
        if own:
            client.close()
