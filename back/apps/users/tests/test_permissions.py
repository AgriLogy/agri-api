"""Tests for the IsAdminOrSelf permission and the IsAdminUser gate
on legacy endpoints."""

import pytest
from django.urls import reverse

from apps.users.permissions import IsAdminOrSelf


class _FakeView:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.mark.django_db
class TestIsAdminOrSelf:
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
class TestLegacyUserListGated:
    """The old /auth/users/ endpoint used to be AllowAny — verify
    the regression fix sticks."""

    def test_anonymous_is_401(self, anon_client):
        url = reverse("user-list")
        resp = anon_client.get(url)
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_client):
        url = reverse("user-list")
        resp = user_client.get(url)
        assert resp.status_code == 403

    def test_admin_succeeds(self, admin_client):
        url = reverse("user-list")
        resp = admin_client.get(url)
        assert resp.status_code == 200
