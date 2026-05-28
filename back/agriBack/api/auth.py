"""JWT authenticator for django-ninja.

Wraps ``djangorestframework-simplejwt``'s token validation so the v2
endpoints accept the same ``Authorization: Bearer <access>`` headers
the legacy DRF views accept. Attaches the resolved Django user to the
request so handlers can read ``request.auth`` for the authenticated
user.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from ninja.security import HttpBearer
from rest_framework_simplejwt.exceptions import (
    InvalidToken,
    TokenError,
)
from rest_framework_simplejwt.tokens import AccessToken


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

        # django-ninja stashes the return value on `request.auth`; we also
        # stick the user on `request.user` so existing helpers that reach
        # for it keep working.
        request.user = user
        return user
