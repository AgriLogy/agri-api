"""Tests for GET/PATCH/DELETE /users/<username> (django-ninja, JWT bearer auth)."""

import pytest
from django.contrib.auth import get_user_model

User = get_user_model()


def _url(username: str) -> str:
    return f"/users/{username}"


@pytest.mark.django_db
class TestAdminUserDetail:
    def test_anonymous_is_401(self, anon_client, normal_user):
        resp = anon_client.get(_url(normal_user.username))
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_bearer, other_user):
        resp = user_bearer.get(_url(other_user.username))
        assert resp.status_code == 403

    def test_admin_can_retrieve(self, admin_bearer, normal_user):
        resp = admin_bearer.get(_url(normal_user.username))
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == normal_user.username
        assert "date_joined" in body
        assert "zones_count" in body

    def test_admin_can_patch_email(self, admin_bearer, normal_user):
        resp = admin_bearer.patch(
            _url(normal_user.username),
            {"email": "newmail@example.com"}, format="json",
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "newmail@example.com"
        normal_user.refresh_from_db()
        assert normal_user.email == "newmail@example.com"

    def test_admin_cannot_set_duplicate_email(
        self, admin_bearer, normal_user, other_user
    ):
        resp = admin_bearer.patch(
            _url(normal_user.username),
            {"email": other_user.email}, format="json",
        )
        assert resp.status_code == 400
        assert "email" in resp.json()

    def test_admin_cannot_change_username(self, admin_bearer, normal_user):
        # username is not part of the update schema, so it is ignored:
        # PATCH succeeds and the username stays unchanged.
        resp = admin_bearer.patch(
            _url(normal_user.username),
            {"username": "renamed"}, format="json",
        )
        assert resp.status_code == 200
        normal_user.refresh_from_db()
        assert normal_user.username != "renamed"

    def test_invalid_latitude_is_400(self, admin_bearer, normal_user):
        resp = admin_bearer.patch(
            _url(normal_user.username),
            {"latitude": -200}, format="json",
        )
        assert resp.status_code == 400

    def test_delete_soft_deletes(self, admin_bearer, normal_user):
        resp = admin_bearer.delete(_url(normal_user.username))
        assert resp.status_code == 204
        normal_user.refresh_from_db()
        assert normal_user.is_active is False

    def test_admin_cannot_delete_self(self, admin_bearer, admin_user):
        resp = admin_bearer.delete(_url(admin_user.username))
        assert resp.status_code == 400

    def test_not_found_returns_404(self, admin_bearer):
        resp = admin_bearer.get(_url("ghost"))
        assert resp.status_code == 404
