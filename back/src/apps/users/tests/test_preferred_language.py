"""Tests for the per-user preferred_language field (agri-api #31):
GET/PATCH /users/me self-service + the admin patch path."""

import pytest


@pytest.mark.django_db
class TestPreferredLanguage:
    def test_me_defaults_to_fr(self, user_bearer, normal_user):
        resp = user_bearer.get("/users/me")
        assert resp.status_code == 200
        assert resp.json()["preferred_language"] == "fr"

    def test_user_can_set_own_language(self, user_bearer, normal_user):
        resp = user_bearer.patch(
            "/users/me", {"preferred_language": "ar"}, format="json"
        )
        assert resp.status_code == 200
        assert resp.json()["preferred_language"] == "ar"
        normal_user.refresh_from_db()
        assert normal_user.preferred_language == "ar"

    def test_user_invalid_language_is_400(self, user_bearer, normal_user):
        resp = user_bearer.patch(
            "/users/me", {"preferred_language": "es"}, format="json"
        )
        assert resp.status_code == 400
        normal_user.refresh_from_db()
        assert normal_user.preferred_language == "fr"

    def test_anonymous_me_is_401(self, anon_client):
        assert anon_client.get("/users/me").status_code == 401

    def test_admin_can_set_user_language(self, admin_bearer, normal_user):
        resp = admin_bearer.patch(
            f"/users/{normal_user.username}",
            {"preferred_language": "ar"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.json()["preferred_language"] == "ar"
        normal_user.refresh_from_db()
        assert normal_user.preferred_language == "ar"

    def test_admin_invalid_language_is_400(self, admin_bearer, normal_user):
        resp = admin_bearer.patch(
            f"/users/{normal_user.username}",
            {"preferred_language": "zz"},
            format="json",
        )
        assert resp.status_code == 400
        normal_user.refresh_from_db()
        assert normal_user.preferred_language == "fr"
