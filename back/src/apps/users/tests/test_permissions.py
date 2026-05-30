"""Tests for the IsAdminOrSelf permission and the admin-gate on the
django-ninja user endpoints (JWT bearer auth)."""

import pytest

from apps.users.permissions import IsAdminOrSelf

# Admin user list/CRUD lives at /users (django-ninja, is_staff-gated inline).
USERS_URL = "/users"


class _FakeView:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.mark.django_db
class TestIsAdminOrSelf:
    """Unit-level coverage of the shared IsAdminOrSelf permission class.

    Still used by self-scoped admin helpers; admin → any user, normal
    user → self only, anonymous → denied."""

    def test_anonymous_denied(self, anon_client, normal_user):
        perm = IsAdminOrSelf()
        request = type(
            "R", (), {"user": type("U", (), {"is_authenticated": False})()}
        )()
        view = _FakeView(username=normal_user.username)
        assert perm.has_permission(request, view) is False

    def test_admin_allowed_for_any_user(self, admin_user, normal_user):
        perm = IsAdminOrSelf()
        request = type("R", (), {"user": admin_user})()
        view = _FakeView(username=normal_user.username)
        assert perm.has_permission(request, view) is True

    def test_user_allowed_for_self(self, normal_user):
        perm = IsAdminOrSelf()
        request = type("R", (), {"user": normal_user})()
        view = _FakeView(username=normal_user.username)
        assert perm.has_permission(request, view) is True

    def test_user_denied_for_other(self, normal_user, other_user):
        perm = IsAdminOrSelf()
        request = type("R", (), {"user": normal_user})()
        view = _FakeView(username=other_user.username)
        assert perm.has_permission(request, view) is False


@pytest.mark.django_db
class TestUserListGated:
    """The user-list endpoint must stay admin-only: anonymous is
    rejected by JWT auth (401) and non-staff users by the inline
    is_staff gate (403)."""

    def test_anonymous_is_401(self, anon_client):
        resp = anon_client.get(USERS_URL)
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_bearer):
        resp = user_bearer.get(USERS_URL)
        assert resp.status_code == 403

    def test_admin_succeeds(self, admin_bearer):
        resp = admin_bearer.get(USERS_URL)
        assert resp.status_code == 200
