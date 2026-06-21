"""JWT authenticator for django-ninja.

Wraps ``djangorestframework-simplejwt``'s token validation so the v2
endpoints accept the same ``Authorization: Bearer <access>`` headers
the legacy DRF views accept. Attaches the resolved Django user to the
request so handlers can read ``request.auth`` for the authenticated
user.

Both authenticators below enforce the admin session kill switch:
``CustomUser.sessions_revoked_at``. A token whose ``iat`` (issued-at) is
older than that timestamp — or a token for a disabled account — is
rejected, which is what powers the admin "force logout" / "disable"
actions. simplejwt stamps every token with ``iat`` at mint time, so no
mint-side change is needed.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from ninja.security import HttpBearer
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import (
    AuthenticationFailed,
    InvalidToken,
    TokenError,
)
from rest_framework_simplejwt.tokens import AccessToken


def token_session_revoked(user, validated_token) -> bool:
    """True if this token should be rejected because the user's sessions were
    revoked after it was issued.

    Driven solely by ``sessions_revoked_at`` — disabling an account bumps that
    timestamp, so disabled users are caught here too without changing the
    pre-existing ``is_active`` handling of the auth layers (the DRF
    authenticator already rejects inactive users via the parent ``get_user``).
    """
    revoked_at = getattr(user, "sessions_revoked_at", None)
    if revoked_at is None:
        return False
    iat = validated_token.get("iat")
    # No iat (shouldn't happen with simplejwt) → fail safe and revoke.
    if iat is None:
        return True
    # Compare at whole-second resolution: JWT `iat` is an integer second, so a
    # token minted in the same second as the revocation (a re-login moments
    # after a force-logout) must not be falsely killed by sub-second precision.
    return iat < int(revoked_at.timestamp())


class JwtAuth(HttpBearer):
    """Validate a simplejwt access token and resolve the Django user."""

    def authenticate(self, request, token):
        try:
            validated = AccessToken(token)
        except (InvalidToken, TokenError):
            return None

        user_id = validated.get("user_id")
        if user_id is None:
            return None

        user_model = get_user_model()
        try:
            user = user_model.objects.get(pk=user_id)
        except user_model.DoesNotExist:
            return None

        if token_session_revoked(user, validated):
            return None

        # django-ninja stashes the return value on `request.auth`; we also
        # stick the user on `request.user` so existing helpers that reach
        # for it keep working.
        request.user = user
        return user


class RevocationAwareJWTAuthentication(JWTAuthentication):
    """DRF authenticator that also honours the session kill switch.

    Set as ``DEFAULT_AUTHENTICATION_CLASSES`` so every legacy DRF view gets
    the same revocation semantics as the django-ninja endpoints. The parent
    ``get_user`` already rejects inactive accounts; we add the
    ``sessions_revoked_at`` check on top.
    """

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if token_session_revoked(user, validated_token):
            raise AuthenticationFailed(
                "Session has been revoked.", code="session_revoked"
            )
        return user
