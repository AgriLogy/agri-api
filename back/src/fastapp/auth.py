"""fastapp authentication — verify Django-issued simplejwt access tokens.

Strangler phase F1: the sidecar accepts the very tokens the Django side
mints (`rest_framework_simplejwt`, HS256 over Django's SECRET_KEY) and
resolves the user from the SAME ``CustomUser_customuser`` table via the
agri-core SQLAlchemy session — so a client can hit a route the moment it
cuts over to :8001 without re-authenticating.

Parity contract (see ``agriapi/api/auth.py`` + SIMPLE_JWT in
``agriapi/settings/base.py``):

* signature: HS256 + SECRET_KEY (simplejwt's default SIGNING_KEY)
* claims: ``token_type`` must be ``"access"`` (TOKEN_TYPE_CLAIM),
  ``user_id`` identifies the user (USER_ID_CLAIM), ``exp``/``iat`` required
* rejects: unknown user, inactive user, and any token issued before
  ``CustomUser.sessions_revoked_at`` (the admin force-logout kill switch)
"""

from __future__ import annotations

import datetime

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from agri.core.database.session import session_scope
from agri.db.users import CustomUserCustomuser
from fastapp.settings import get_settings


class AuthedUser(BaseModel):
    """The authenticated caller, as routes see it (a detached snapshot —
    no live ORM instance escapes the session)."""

    id: int
    username: str
    email: str
    is_staff: bool
    is_technician: bool = False
    preferred_language: str


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


# auto_error=False so a missing/naked Authorization header yields our own
# 401 (+ WWW-Authenticate) instead of HTTPBearer's stock 403.
_bearer = HTTPBearer(auto_error=False)


def token_session_revoked(
    sessions_revoked_at: datetime.datetime | None, iat: int | None
) -> bool:
    """True if this token should be rejected because the user's sessions were
    revoked after it was issued.

    Ported EXACTLY from ``agriapi.api.auth.token_session_revoked`` (the
    Django original) — a later phase lifts both copies into agri-core so the
    semantics can never drift. Keep the two in lockstep until then.
    """
    if sessions_revoked_at is None:
        return False
    # No iat (shouldn't happen with simplejwt) → fail safe and revoke.
    if iat is None:
        return True
    # Compare at whole-second resolution: JWT `iat` is an integer second, so a
    # token minted in the same second as the revocation (a re-login moments
    # after a force-logout) must not be falsely killed by sub-second precision.
    return iat < int(sessions_revoked_at.timestamp())


def decode_access_token(token: str) -> dict:
    """Validate signature/expiry and the simplejwt claim contract; return the
    claims. Raises HTTPException(401) on any failure."""
    try:
        claims = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=["HS256"],
            options={"require": ["exp", "iat"]},
        )
    except jwt.PyJWTError:
        raise _unauthorized("Token is invalid or expired.") from None

    # SIMPLE_JWT TOKEN_TYPE_CLAIM: only *access* tokens authenticate requests
    # (a refresh token must never work as a bearer credential).
    if claims.get("token_type") != "access":
        raise _unauthorized("Token has wrong type.")
    if claims.get("user_id") is None:
        raise _unauthorized("Token contained no recognizable user identification.")
    return claims


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthedUser:
    """FastAPI dependency: Bearer token → AuthedUser (or 401)."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("Authentication credentials were not provided.")

    claims = decode_access_token(credentials.credentials)

    with session_scope() as session:
        user = session.get(CustomUserCustomuser, claims["user_id"])
        if user is None:
            raise _unauthorized("User not found.")
        if not user.is_active:
            raise _unauthorized("User is inactive.")
        if token_session_revoked(user.sessions_revoked_at, claims.get("iat")):
            raise _unauthorized("Session has been revoked.")
        return AuthedUser(
            id=user.id,
            username=user.username,
            email=user.email,
            is_staff=user.is_staff,
            is_technician=user.is_technician,
            preferred_language=user.preferred_language,
        )


def get_current_staff_user(
    user: AuthedUser = Depends(get_current_user),
) -> AuthedUser:
    """FastAPI dependency: like get_current_user, but 403 for non-staff."""
    if not user.is_staff:
        raise HTTPException(
            status_code=403, detail="You do not have permission to perform this action."
        )
    return user
