"""Custom middleware for agri-api."""

from __future__ import annotations

from django.http import JsonResponse
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class ImpersonationReadOnlyMiddleware:
    """Reject mutating requests made with a read-only impersonation token.

    Admins can mint a short-lived "view-as" token (see
    ``apps.irrigation.router_impersonation``) that authenticates as a target
    user but carries a truthy ``readonly`` claim. This middleware runs before
    routing, decodes the bearer token, and returns 403 for any non-safe HTTP
    method when that claim is set — so an admin viewing a user's account can
    never change that account's data, regardless of which endpoint (django-ninja
    or legacy DRF) is hit.

    Fail-open on decode: an invalid/absent token is left for the normal auth
    layer to reject; we only ever ADD a restriction, never grant access.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in SAFE_METHODS:
            auth = request.META.get("HTTP_AUTHORIZATION", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
                try:
                    validated = AccessToken(token)
                except (InvalidToken, TokenError):
                    validated = None
                if validated is not None and validated.get("readonly"):
                    return JsonResponse(
                        {
                            "detail": (
                                "Read-only impersonation session: mutations are "
                                "not allowed."
                            )
                        },
                        status=403,
                    )
        return self.get_response(request)
