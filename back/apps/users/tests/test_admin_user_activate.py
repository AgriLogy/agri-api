"""Tests for POST /users/<username>/activate (django-ninja, JWT bearer auth)."""

import pytest


def url(username: str) -> str:
    return f"/users/{username}/activate"


@pytest.mark.django_db
class TestAdminUserActivate:
    def test_anonymous_is_401(self, anon_client, normal_user):
        resp = anon_client.post(url(normal_user.username))
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_bearer, other_user):
        resp = user_bearer.post(url(other_user.username), {}, format="json")
        assert resp.status_code == 403

    def test_toggle_without_body_flips_state(self, admin_bearer, normal_user):
        assert normal_user.is_active is True

        resp = admin_bearer.post(url(normal_user.username), {}, format="json")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        normal_user.refresh_from_db()
        assert normal_user.is_active is False

        resp = admin_bearer.post(url(normal_user.username), {}, format="json")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True
        normal_user.refresh_from_db()
        assert normal_user.is_active is True

    def test_explicit_body_sets_state(self, admin_bearer, normal_user):
        resp = admin_bearer.post(
            url(normal_user.username), {"is_active": False}, format="json"
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        normal_user.refresh_from_db()
        assert normal_user.is_active is False

    def test_explicit_body_must_be_bool(self, admin_bearer, normal_user):
        resp = admin_bearer.post(
            url(normal_user.username), {"is_active": "maybe"}, format="json"
        )
        assert resp.status_code == 422

    def test_admin_cannot_deactivate_self(self, admin_bearer, admin_user):
        resp = admin_bearer.post(
            url(admin_user.username), {"is_active": False}, format="json"
        )
        assert resp.status_code == 400

    def test_not_found_returns_404(self, admin_bearer):
        resp = admin_bearer.post(url("ghost"), {}, format="json")
        assert resp.status_code == 404
