"""Admin impersonation router (django-ninja) — mounted at ``/admin/impersonate``.

Lets a staff user start a short-lived, **read-only** "view-as" session for any
user: it mints a simplejwt access token that authenticates AS the target user
but carries a ``readonly`` claim. ``agriapi.middleware.ImpersonationReadOnlyMiddleware``
rejects every mutating request made with such a token, so the admin can browse
the user's data (e.g. in the farmer web app) but can never change it.

Every session start is audit-logged. There is no "stop" endpoint: the token is
short-lived and the client simply discards it to exit (a read-only token could
not POST a stop anyway — the middleware would block it).
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from ninja import Router
from ninja.responses import Response
from rest_framework_simplejwt.tokens import AccessToken

from agriapi.api.auth import JwtAuth
from apps.irrigation.audit import record_audit
from apps.irrigation.router_admin import _require_admin

router = Router()
User = get_user_model()

# Read-only view-as tokens are deliberately short-lived.
IMPERSONATION_TTL_MINUTES = 30


@router.post(
    "/{username}",
    auth=JwtAuth(),
    summary="Admin: start a read-only view-as session for a user",
)
def impersonate(request, username: str):
    guard = _require_admin(request)
    if guard:
        return guard

    target = User.objects.filter(username=username).first()
    if target is None:
        return Response({"detail": "User not found"}, status=404)

    token = AccessToken.for_user(target)
    token["readonly"] = True
    token["impersonator"] = getattr(request.auth, "pk", None)
    token["impersonator_username"] = getattr(request.auth, "username", None)
    token.set_exp(lifetime=timedelta(minutes=IMPERSONATION_TTL_MINUTES))

    record_audit(
        request.auth,
        "impersonate.start",
        "user",
        username,
        {"impersonator": getattr(request.auth, "username", None)},
    )

    return {
        "access": str(token),
        "username": target.username,
        "readonly": True,
        "expires_in": IMPERSONATION_TTL_MINUTES * 60,
    }
