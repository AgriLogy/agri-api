"""Tests for POST /auth/admin/users/<username>/reset-password/."""

import pytest
from django.urls import reverse


@pytest.mark.django_db
class TestAdminResetPassword:
    def test_anonymous_is_401(self, anon_client, normal_user):
        url = reverse(
            "admin-user-reset-password", kwargs={"username": normal_user.username}
        )
        resp = anon_client.post(url)
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_client, other_user):
        url = reverse(
            "admin-user-reset-password", kwargs={"username": other_user.username}
        )
        resp = user_client.post(url)
        assert resp.status_code == 403

    def test_admin_explicit_password(self, admin_client, normal_user):
        url = reverse(
            "admin-user-reset-password", kwargs={"username": normal_user.username}
        )
        resp = admin_client.post(
            url, {"password": "An0ther-Sup3r-S3cret!"}, format="json"
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["username"] == normal_user.username
        normal_user.refresh_from_db()
        assert normal_user.check_password("An0ther-Sup3r-S3cret!")

    def test_admin_generated_password(self, admin_client, normal_user):
        url = reverse(
            "admin-user-reset-password", kwargs={"username": normal_user.username}
        )
        resp = admin_client.post(url, {}, format="json")
        assert resp.status_code == 200
        body = resp.json()
        assert "password" in body and isinstance(body["password"], str)
        assert len(body["password"]) >= 10
        normal_user.refresh_from_db()
        assert normal_user.check_password(body["password"])

    def test_weak_password_rejected(self, admin_client, normal_user):
        url = reverse(
            "admin-user-reset-password", kwargs={"username": normal_user.username}
        )
        resp = admin_client.post(url, {"password": "12"}, format="json")
        assert resp.status_code == 400
        assert "password" in resp.json()

    def test_not_found_returns_404(self, admin_client):
        url = reverse("admin-user-reset-password", kwargs={"username": "ghost"})
        resp = admin_client.post(url)
        assert resp.status_code == 404
