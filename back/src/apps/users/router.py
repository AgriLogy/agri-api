"""Auth + user-management router (django-ninja).

Mounted from ``agriapi.api`` at ``/auth``. Each endpoint matches the
URL the legacy DRF ``apps.users.urls`` exposed so the frontend keeps
working without changes.

What stays in ``apps.users.urls`` (still DRF):
  * ``POST /auth/token/``         — simplejwt ``TokenObtainPairView``
  * ``POST /auth/token/refresh/`` — simplejwt ``TokenRefreshView``
  * ``/auth/admin/...``           — the admin sub-tree (PR 10 target)

Migration notes:
  * Validation-error envelopes shift from ad-hoc DRF dicts to django-ninja's
    standard 422 for body shape errors. Response shapes for happy paths are
    preserved byte-for-byte so the dashboard keeps parsing.
  * Rate-limited login retains its 5-attempt / 5-minute lock-out via the
    Django cache key ``login_attempts_<username>``.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from ninja import Router, Schema
from ninja.responses import Response
from rest_framework_simplejwt.tokens import RefreshToken

from agriapi.api.auth import JwtAuth
from apps.users.models import CustomUser
from apps.users.notification_helper import perform_calculations

router = Router()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LOGIN_LOCKOUT_CACHE_PREFIX = "login_attempts_"
_LOGIN_LOCKOUT_LIMIT = 5
_LOGIN_LOCKOUT_TIMEOUT_S = 300


def _require_admin(request) -> CustomUser | Response | None:
    """Mirror DRF's ``IsAdminUser`` check; returns a 403 ``Response`` when
    the JWT user is not staff. JwtAuth already enforces authentication."""
    user = request.auth
    if user is None or not getattr(user, "is_staff", False):
        return Response({"detail": "Admin access required"}, status=403)
    return None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SignUpIn(Schema):
    username: str
    email: str
    firstname: str
    lastname: str
    phone_number: str
    password: str


class AdminSignUpIn(SignUpIn):
    is_staff: bool = False


class SignInIn(Schema):
    username: str
    password: str


class SignInOut(Schema):
    refresh: str
    access: str
    is_staff: bool
    is_technician: bool = False


class AdminSignInOut(Schema):
    refresh: str
    access: str


class AdminUserOut(Schema):
    username: str
    email: str | None = None
    phone_number: str | None = None
    payement_status: str | None = None
    is_staff: bool


class AdminModifyUserOut(Schema):
    username: str
    email: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    phone_number: str | None = None
    payement_status: str | None = None
    is_staff: bool
    longitude: float | None = None
    latitude: float | None = None


class AdminModifyUserIn(Schema):
    """All fields optional — partial update keyed by ``username``."""

    username: str  # which user to modify
    email: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    phone_number: str | None = None
    payement_status: str | None = None
    is_staff: bool | None = None
    longitude: float | None = None
    latitude: float | None = None


# ---------------------------------------------------------------------------
# Signup / signin
# ---------------------------------------------------------------------------


@router.post("/signup", auth=None, summary="Public user signup")
def signup(request, payload: SignUpIn):
    if CustomUser.objects.filter(email=payload.email).exists():
        return Response({"email": "This email is already in use."}, status=400)
    if CustomUser.objects.filter(username=payload.username).exists():
        return Response({"username": "This username is already in use."}, status=400)
    try:
        validate_password(payload.password)
    except ValidationError as exc:
        return Response({"password": exc.messages}, status=400)

    user = CustomUser(
        username=payload.username,
        email=payload.email,
        firstname=payload.firstname,
        lastname=payload.lastname,
        phone_number=payload.phone_number,
    )
    user.set_password(payload.password)
    user.save()
    return Response({"status": "Account created successfully"}, status=201)


def _record_login(request, username: str, user, success: bool) -> None:
    """Best-effort sign-in event recording for the monitoring back-office.

    Never raises — the (unmanaged, self-deployed) analytics_loginevent table
    may be absent and must not break authentication.
    """
    try:
        from apps.irrigation.models import LoginEvent

        meta = getattr(request, "META", {}) or {}
        ip = meta.get("HTTP_X_FORWARDED_FOR", "") or meta.get("REMOTE_ADDR", "") or ""
        ip = ip.split(",")[0].strip()
        LoginEvent.objects.create(
            username=(username or "")[:150],
            user=user if getattr(user, "pk", None) else None,
            success=success,
            ip=ip[:64],
            user_agent=(meta.get("HTTP_USER_AGENT", "") or "")[:255],
        )
    except Exception:  # pragma: no cover - fail-soft
        pass


def _signin_core(request, payload: SignInIn) -> Response:
    if not payload.username or not payload.password:
        return Response(
            {"error": "Username and password are required."},
            status=400,
        )

    cache_key = f"{_LOGIN_LOCKOUT_CACHE_PREFIX}{payload.username}"
    attempts = cache.get(cache_key, 0)
    if attempts >= _LOGIN_LOCKOUT_LIMIT:
        return Response(
            {"error": "Too many login attempts. Please try again later."},
            status=429,
        )

    user = authenticate(None, username=payload.username, password=payload.password)
    if user is None:
        cache.set(cache_key, attempts + 1, timeout=_LOGIN_LOCKOUT_TIMEOUT_S)
        _record_login(request, payload.username, None, success=False)
        return Response({"error": "Invalid credentials"}, status=401)
    cache.delete(cache_key)
    _record_login(request, payload.username, user, success=True)
    refresh = RefreshToken.for_user(user)
    return Response(
        {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "is_staff": user.is_staff,
            "is_technician": getattr(user, "is_technician", False),
        },
        status=200,
    )


@router.post("/sessions", auth=None, summary="Public user sign-in (issue JWT)")
def signin(request, payload: SignInIn):
    response = _signin_core(request, payload)
    # The legacy DRF view called Django's login() to set a session cookie too.
    # Mirror that for parity with the frontend's "remember me" behaviour.
    if response.status_code == 200:
        user = CustomUser.objects.get(username=payload.username)
        login(request, user)
    return response


@router.post(
    "/admin-sessions", auth=None, summary="Admin-only sign-in (response omits is_staff)"
)
def admin_signin(request, payload: SignInIn):
    """Same flow as ``/signin/`` but the response omits ``is_staff``. Matches
    the legacy ``AdminSignInAPIView`` shape so the admin login page keeps
    parsing the response unchanged."""
    response = _signin_core(request, payload)
    if response.status_code == 200:
        body = response.content
        # Strip is_staff out for the admin endpoint's response shape.
        import json as _json

        decoded = _json.loads(body)
        decoded.pop("is_staff", None)
        return Response(decoded, status=200)
    return response


@router.delete(
    "/sessions",
    auth=JwtAuth(),
    summary="Log out everywhere (revoke all of the caller's own sessions)",
)
def logout_everywhere(request):
    """Self-service global logout — the user-driven twin of the admin
    ``force-logout``. Bumps the caller's own ``sessions_revoked_at`` so every
    token issued so far is rejected: the current access token fails on its next
    request and the refresh token can no longer mint a new one. This is what
    makes one "log out" take effect across every Agrogo app that shares the SSO
    session, not just the tab the user clicked in. Clearing the local cookie
    only stops re-hydration on this browser; revoking here ends the session on
    the server for siblings too."""
    user = request.auth
    user.sessions_revoked_at = now()
    user.save(update_fields=["sessions_revoked_at"])
    log.info("user %s logged out everywhere (sessions revoked)", user.username)
    return {
        "success": True,
        "sessions_revoked_at": user.sessions_revoked_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Admin signup, admin user list, modify-user moved to apps.users.router_admin
# which mounts at /users (the unified user-resources surface).
# Send-notification moves to POST /users/me/notifications (also in router_admin).
# ---------------------------------------------------------------------------


def _send_notification(request):  # retained as a helper; no longer routed here
    user = request.auth
    recipient = (getattr(user, "email", "") or "").strip()
    if not recipient:
        return Response(
            {"success": False, "error": "user has no email address on file"},
            status=400,
        )
    try:
        message = perform_calculations(user)
        send_mail(
            subject="Mise à jour de votre terrain agricole",
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=False,
        )
        user.last_notified = now()
        user.save(update_fields=["last_notified"])
        log.info("Sent on-demand notification email to %s", recipient)
        return {"success": True, "message": "Email envoyé avec succès."}
    except Exception:
        log.exception("Failed to send notification email to %s", recipient)
        return Response(
            {"success": False, "error": "Failed to send notification email."},
            status=500,
        )
