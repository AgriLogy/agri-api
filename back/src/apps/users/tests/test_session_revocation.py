"""Tests for the admin session kill switch (CustomUser.sessions_revoked_at).

Covers, end to end, the "force logout" / "disable account" feature:

* ``token_session_revoked`` — the pure predicate.
* ``JwtAuth`` (django-ninja) — revocation enforced, ``is_active`` *not* (so the
  pre-existing technician behaviour is preserved).
* ``RevocationAwareJWTAuthentication`` (DRF) — revocation + inactive rejection.
* ``RevocationAwareTokenRefreshView`` — revoked / disabled refresh is blocked.
* ``GET /users/{u}/sessions`` — derived token status.
* ``POST /users/{u}/force-logout`` — revokes, with permission + 404 guards.
* Disabling (activate→False, soft-delete) also revokes.

Token ``iat`` values are set explicitly so revocation tests never depend on
wall-clock timing within a single second.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from agriapi.api.auth import (
    JwtAuth,
    RevocationAwareJWTAuthentication,
    token_session_revoked,
)

ME_URL = "/users/me"
REFRESH_URL = "/auth/token/refresh/"


def _sessions_url(username: str) -> str:
    return f"/users/{username}/sessions"


def _force_logout_url(username: str) -> str:
    return f"/users/{username}/force-logout"


def _access_with_iat(user, iat_dt) -> AccessToken:
    token = AccessToken.for_user(user)
    token["iat"] = int(iat_dt.timestamp())
    return token


def _bearer(user, iat_dt=None) -> APIClient:
    """APIClient with a Bearer access token, optionally minted at ``iat_dt``."""
    iat_dt = iat_dt or timezone.now()
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_access_with_iat(user, iat_dt)}")
    return client


def _refresh_with_iat(user, iat_dt) -> str:
    token = RefreshToken.for_user(user)
    token["iat"] = int(iat_dt.timestamp())
    return str(token)


# ---------------------------------------------------------------------------
# token_session_revoked — the pure predicate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTokenSessionRevokedPredicate:
    def test_no_revocation_timestamp_is_not_revoked(self, normal_user):
        assert normal_user.sessions_revoked_at is None
        token = AccessToken.for_user(normal_user)
        assert token_session_revoked(normal_user, token) is False

    def test_token_older_than_revocation_is_revoked(self, normal_user):
        now = timezone.now()
        normal_user.sessions_revoked_at = now
        token = _access_with_iat(normal_user, now - timedelta(minutes=5))
        assert token_session_revoked(normal_user, token) is True

    def test_token_newer_than_revocation_is_not_revoked(self, normal_user):
        now = timezone.now()
        normal_user.sessions_revoked_at = now - timedelta(minutes=5)
        token = _access_with_iat(normal_user, now)
        assert token_session_revoked(normal_user, token) is False

    def test_token_iat_equal_to_revocation_is_not_revoked(self, normal_user):
        now = timezone.now().replace(microsecond=0)
        normal_user.sessions_revoked_at = now
        token = _access_with_iat(normal_user, now)
        # Strict `<` — a token minted at exactly the revocation second survives.
        assert token_session_revoked(normal_user, token) is False

    def test_missing_iat_fails_safe_to_revoked(self, normal_user):
        normal_user.sessions_revoked_at = timezone.now()
        assert token_session_revoked(normal_user, {}) is True

    def test_inactive_user_without_revocation_is_not_revoked(self, normal_user):
        # is_active is intentionally NOT part of this predicate.
        normal_user.is_active = False
        token = AccessToken.for_user(normal_user)
        assert token_session_revoked(normal_user, token) is False


# ---------------------------------------------------------------------------
# JwtAuth (django-ninja) — via the JwtAuth-protected /users/me endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestJwtAuthRevocation:
    def test_valid_token_authenticates(self, normal_user):
        resp = _bearer(normal_user).get(ME_URL)
        assert resp.status_code == 200
        assert resp.json()["username"] == normal_user.username

    def test_token_before_revocation_is_rejected(self, normal_user):
        now = timezone.now()
        client = _bearer(normal_user, iat_dt=now - timedelta(minutes=5))
        normal_user.sessions_revoked_at = now
        normal_user.save(update_fields=["sessions_revoked_at"])
        assert client.get(ME_URL).status_code == 401

    def test_token_after_revocation_still_works(self, normal_user):
        now = timezone.now()
        normal_user.sessions_revoked_at = now - timedelta(minutes=5)
        normal_user.save(update_fields=["sessions_revoked_at"])
        # Fresh login mints a token *after* the revocation → still valid.
        client = _bearer(normal_user, iat_dt=now)
        assert client.get(ME_URL).status_code == 200

    def test_inactive_user_without_revocation_still_authenticates(self, normal_user):
        # Regression guard: ninja JwtAuth historically ignores is_active, which
        # the technician-revoke flow relies on. Adding the kill switch must not
        # change that — only sessions_revoked_at gates ninja auth.
        normal_user.is_active = False
        normal_user.save(update_fields=["is_active"])
        assert _bearer(normal_user).get(ME_URL).status_code == 200

    def test_jwtauth_authenticate_returns_none_when_revoked(self, rf, normal_user):
        now = timezone.now()
        token = _access_with_iat(normal_user, now - timedelta(minutes=1))
        normal_user.sessions_revoked_at = now
        normal_user.save(update_fields=["sessions_revoked_at"])
        request = rf.get(ME_URL)
        assert JwtAuth().authenticate(request, str(token)) is None


# ---------------------------------------------------------------------------
# RevocationAwareJWTAuthentication (DRF default authenticator)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDRFAuthenticatorRevocation:
    def test_valid_token_resolves_user(self, normal_user):
        auth = RevocationAwareJWTAuthentication()
        token = AccessToken.for_user(normal_user)
        assert auth.get_user(token) == normal_user

    def test_revoked_token_raises(self, normal_user):
        now = timezone.now()
        normal_user.sessions_revoked_at = now
        normal_user.save(update_fields=["sessions_revoked_at"])
        token = _access_with_iat(normal_user, now - timedelta(minutes=1))
        auth = RevocationAwareJWTAuthentication()
        with pytest.raises(AuthenticationFailed):
            auth.get_user(token)

    def test_inactive_user_raises(self, normal_user):
        # Parent simplejwt behaviour — disabled accounts are rejected on DRF.
        normal_user.is_active = False
        normal_user.save(update_fields=["is_active"])
        token = AccessToken.for_user(normal_user)
        auth = RevocationAwareJWTAuthentication()
        with pytest.raises(AuthenticationFailed):
            auth.get_user(token)


# ---------------------------------------------------------------------------
# RevocationAwareTokenRefreshView — POST /auth/token/refresh/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTokenRefreshRevocation:
    def test_normal_refresh_succeeds(self, normal_user):
        refresh = str(RefreshToken.for_user(normal_user))
        resp = APIClient().post(REFRESH_URL, {"refresh": refresh}, format="json")
        assert resp.status_code == 200
        assert "access" in resp.json()

    def test_refresh_before_revocation_is_rejected(self, normal_user):
        now = timezone.now()
        refresh = _refresh_with_iat(normal_user, now - timedelta(minutes=5))
        normal_user.sessions_revoked_at = now
        normal_user.save(update_fields=["sessions_revoked_at"])
        resp = APIClient().post(REFRESH_URL, {"refresh": refresh}, format="json")
        assert resp.status_code == 401

    def test_refresh_after_revocation_succeeds(self, normal_user):
        now = timezone.now()
        normal_user.sessions_revoked_at = now - timedelta(minutes=5)
        normal_user.save(update_fields=["sessions_revoked_at"])
        refresh = _refresh_with_iat(normal_user, now)
        resp = APIClient().post(REFRESH_URL, {"refresh": refresh}, format="json")
        assert resp.status_code == 200

    def test_refresh_for_disabled_account_is_rejected(self, normal_user):
        refresh = str(RefreshToken.for_user(normal_user))
        normal_user.is_active = False
        normal_user.save(update_fields=["is_active"])
        resp = APIClient().post(REFRESH_URL, {"refresh": refresh}, format="json")
        assert resp.status_code == 401

    def test_garbage_token_is_rejected(self):
        resp = APIClient().post(
            REFRESH_URL, {"refresh": "not.a.valid.token"}, format="json"
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /users/{username}/sessions — derived token status
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUserSessionsStatus:
    def test_anonymous_is_401(self, anon_client, normal_user):
        assert anon_client.get(_sessions_url(normal_user.username)).status_code == 401

    def test_normal_user_is_403(self, user_bearer, other_user):
        assert user_bearer.get(_sessions_url(other_user.username)).status_code == 403

    def test_unknown_user_is_404(self, admin_bearer):
        assert admin_bearer.get(_sessions_url("ghost")).status_code == 404

    def test_active_session_status(self, admin_bearer, normal_user):
        normal_user.last_login = timezone.now()
        normal_user.save(update_fields=["last_login"])
        body = admin_bearer.get(_sessions_url(normal_user.username)).json()
        assert body["status"] == "active"
        assert body["last_login"] is not None
        assert body["current_token_expires_at"] is not None
        assert body["access_token_lifetime_minutes"] > 0
        assert body["sessions_revoked_at"] is None

    def test_never_logged_in_status(self, admin_bearer, normal_user):
        assert normal_user.last_login is None
        body = admin_bearer.get(_sessions_url(normal_user.username)).json()
        assert body["status"] == "never_logged_in"
        assert body["current_token_expires_at"] is None

    def test_revoked_status(self, admin_bearer, normal_user):
        now = timezone.now()
        normal_user.last_login = now - timedelta(hours=1)
        normal_user.sessions_revoked_at = now
        normal_user.save(update_fields=["last_login", "sessions_revoked_at"])
        body = admin_bearer.get(_sessions_url(normal_user.username)).json()
        assert body["status"] == "revoked"
        assert body["current_token_expires_at"] is None

    def test_disabled_status(self, admin_bearer, normal_user):
        normal_user.last_login = timezone.now()
        normal_user.is_active = False
        normal_user.save(update_fields=["last_login", "is_active"])
        body = admin_bearer.get(_sessions_url(normal_user.username)).json()
        assert body["status"] == "disabled"


# ---------------------------------------------------------------------------
# POST /users/{username}/force-logout
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestForceLogout:
    def test_anonymous_is_401(self, anon_client, normal_user):
        resp = anon_client.post(_force_logout_url(normal_user.username))
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_bearer, other_user):
        resp = user_bearer.post(_force_logout_url(other_user.username))
        assert resp.status_code == 403

    def test_unknown_user_is_404(self, admin_bearer):
        assert admin_bearer.post(_force_logout_url("ghost")).status_code == 404

    def test_sets_revocation_and_returns_revoked_status(
        self, admin_bearer, normal_user
    ):
        assert normal_user.sessions_revoked_at is None
        resp = admin_bearer.post(_force_logout_url(normal_user.username))
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"
        normal_user.refresh_from_db()
        assert normal_user.sessions_revoked_at is not None

    def test_force_logout_keeps_account_active(self, admin_bearer, normal_user):
        admin_bearer.post(_force_logout_url(normal_user.username))
        normal_user.refresh_from_db()
        assert normal_user.is_active is True

    def test_existing_token_rejected_after_force_logout(
        self, admin_bearer, normal_user
    ):
        # Token minted 5 minutes ago; force-logout stamps "now" → it predates
        # the revocation and is rejected on the next request.
        old_client = _bearer(normal_user, iat_dt=timezone.now() - timedelta(minutes=5))
        assert old_client.get(ME_URL).status_code == 200
        admin_bearer.post(_force_logout_url(normal_user.username))
        assert old_client.get(ME_URL).status_code == 401

    def test_fresh_login_after_force_logout_works(self, admin_bearer, normal_user):
        admin_bearer.post(_force_logout_url(normal_user.username))
        normal_user.refresh_from_db()
        assert normal_user.sessions_revoked_at is not None
        # A real re-login mints a token "now" — at or after the revocation
        # second — so it authenticates (whole-second comparison).
        assert _bearer(normal_user).get(ME_URL).status_code == 200


# ---------------------------------------------------------------------------
# Disabling an account also revokes sessions
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDisableAlsoRevokes:
    def _activate_url(self, username: str) -> str:
        return f"/users/{username}/activate"

    def test_disable_via_activate_sets_revocation(self, admin_bearer, normal_user):
        assert normal_user.sessions_revoked_at is None
        resp = admin_bearer.post(
            self._activate_url(normal_user.username),
            {"is_active": False},
            format="json",
        )
        assert resp.status_code == 200
        normal_user.refresh_from_db()
        assert normal_user.is_active is False
        assert normal_user.sessions_revoked_at is not None

    def test_reenable_does_not_clear_revocation(self, admin_bearer, normal_user):
        admin_bearer.post(
            self._activate_url(normal_user.username),
            {"is_active": False},
            format="json",
        )
        normal_user.refresh_from_db()
        revoked_at = normal_user.sessions_revoked_at
        assert revoked_at is not None

        admin_bearer.post(
            self._activate_url(normal_user.username),
            {"is_active": True},
            format="json",
        )
        normal_user.refresh_from_db()
        assert normal_user.is_active is True
        # Re-enabling must not resurrect old sessions.
        assert normal_user.sessions_revoked_at == revoked_at

    def test_enabling_already_active_user_does_not_revoke(
        self, admin_bearer, normal_user
    ):
        # Toggling an active user to active again (no-op disable) shouldn't stamp.
        admin_bearer.post(
            self._activate_url(normal_user.username),
            {"is_active": True},
            format="json",
        )
        normal_user.refresh_from_db()
        assert normal_user.sessions_revoked_at is None

    def test_soft_delete_sets_revocation(self, admin_bearer, normal_user):
        resp = admin_bearer.delete(f"/users/{normal_user.username}")
        assert resp.status_code == 204
        normal_user.refresh_from_db()
        assert normal_user.is_active is False
        assert normal_user.sessions_revoked_at is not None
