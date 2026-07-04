"""fastapp /admin/impersonate — read-only "view-as" sessions (staff only).

Strangler port of ``apps/irrigation/router_impersonation.py`` (django-ninja).
Mints a short-lived simplejwt *access* token that authenticates AS the target
user but carries a ``readonly`` claim (Django's
``ImpersonationReadOnlyMiddleware`` rejects every mutating request made with
such a token). Every session start is audited.

The minted token is inherently non-deterministic (random ``jti``, time-based
``iat``/``exp``), so the ``access`` string is NOT byte-compared with Django —
the token instead carries the SAME claims simplejwt's ``AccessToken.for_user``
would, decodable + verifiable against the shared ``SECRET_KEY``. The 404 and
403 envelopes, and the rest of the response shape, are byte-parity.
"""

from __future__ import annotations

import datetime
import uuid

import jwt
from fastapi import APIRouter, Depends
from sqlalchemy import select

from agri.core.database import session_scope
from agri.db.users import CustomUserCustomuser
from fastapp.adminutil import record_audit
from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.json import DjangoStyleJSONResponse
from fastapp.settings import get_settings

router = APIRouter(tags=["admin-impersonate"])

# Read-only view-as tokens are deliberately short-lived (mirrors Django).
IMPERSONATION_TTL_MINUTES = 30


def _epoch(dt: datetime.datetime) -> int:
    """simplejwt ``datetime_to_epoch`` — whole seconds (microseconds dropped)."""
    return int(dt.timestamp())


@router.post(
    "/admin/impersonate/{username}", summary="Admin: start a read-only view-as session"
)
def impersonate(username: str, user: AuthedUser = Depends(get_current_staff_user)):
    with session_scope() as session:
        target = session.scalars(
            select(CustomUserCustomuser).where(
                CustomUserCustomuser.username == username
            )
        ).first()
        if target is None:
            return DjangoStyleJSONResponse(
                {"detail": "User not found"}, status_code=404
            )
        target_id = target.id
        target_username = target.username

    now = datetime.datetime.now(datetime.timezone.utc)
    # Mirror simplejwt AccessToken.for_user(target) + the router's extra claims,
    # then set_exp(30min). Claim set/values match; order is irrelevant (the
    # token is signature-verified, never byte-compared).
    claims = {
        "token_type": "access",
        "exp": _epoch(now + datetime.timedelta(minutes=IMPERSONATION_TTL_MINUTES)),
        "iat": _epoch(now),
        "jti": uuid.uuid4().hex,
        "user_id": target_id,
        "readonly": True,
        "impersonator": user.id,
        "impersonator_username": user.username,
    }
    token = jwt.encode(claims, get_settings().secret_key, algorithm="HS256")

    with session_scope(commit=True) as session:
        record_audit(
            session,
            user.id,
            "impersonate.start",
            "user",
            username,
            {"impersonator": user.username},
        )

    return {
        "access": token,
        "username": target_username,
        "readonly": True,
        "expires_in": IMPERSONATION_TTL_MINUTES * 60,
    }
