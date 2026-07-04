"""simplejwt-compatible token minting for the fastapp auth surface.

Mirrors ``rest_framework_simplejwt.RefreshToken.for_user(user)``: HS256 over
Django's ``SECRET_KEY`` (simplejwt's default ``SIGNING_KEY``), the same claim
set — ``token_type`` / ``exp`` / ``iat`` / ``jti`` / ``user_id`` — and the
lifetimes configured in ``agriapi/settings/base.py`` (``SIMPLE_JWT``: access
5 days, refresh 10 days, ``ROTATE_REFRESH_TOKENS`` False).

Tokens minted here are accepted by BOTH ``fastapp.auth.decode_access_token``
and Django's ``rest_framework_simplejwt`` verification, so the two apps stay
cross-compatible during the strangler overlap. ``jti`` is random per token, so
tokens are never byte-identical to a Django mint — parity is by decoded claims.
"""

from __future__ import annotations

import datetime
import uuid

import jwt

from fastapp.settings import get_settings

# SIMPLE_JWT (agriapi/settings/base.py). Kept as constants — static config.
ACCESS_TOKEN_LIFETIME = datetime.timedelta(days=5)
REFRESH_TOKEN_LIFETIME = datetime.timedelta(days=10)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _encode(claims: dict) -> str:
    return jwt.encode(claims, get_settings().secret_key, algorithm="HS256")


def _access_claims(user_id: int, iat: int) -> dict:
    return {
        "token_type": "access",
        "exp": iat + int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        "iat": iat,
        "jti": uuid.uuid4().hex,
        "user_id": user_id,
    }


def _refresh_claims(user_id: int, iat: int) -> dict:
    return {
        "token_type": "refresh",
        "exp": iat + int(REFRESH_TOKEN_LIFETIME.total_seconds()),
        "iat": iat,
        "jti": uuid.uuid4().hex,
        "user_id": user_id,
    }


def mint_tokens(user_id: int) -> tuple[str, str]:
    """Return ``(refresh, access)`` for a user — the pair simplejwt's
    ``RefreshToken.for_user`` + ``.access_token`` would produce."""
    iat = int(_now().timestamp())
    return _encode(_refresh_claims(user_id, iat)), _encode(_access_claims(user_id, iat))


def mint_access(user_id: int) -> str:
    """Return a fresh access token (used by the refresh endpoint)."""
    return _encode(_access_claims(user_id, int(_now().timestamp())))
