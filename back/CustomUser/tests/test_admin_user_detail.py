"""Tests for GET/PATCH/DELETE /auth/admin/users/<username>/."""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

User = get_user_model()


@pytest.mark.django_db
class TestAdminUserDetail:
    def test_anonymous_is_401(self, anon_client, normal_user):
        url = reverse("admin-user-detail", kwargs={"username": normal_user.username})
        resp = anon_client.get(url)
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_client, other_user):
        url = reverse("admin-user-detail", kwargs={"username": other_user.username})
        resp = user_client.get(url)
        assert resp.status_code == 403

    def test_admin_can_retrieve(self, admin_client, normal_user):
        url = reverse("admin-user-detail", kwargs={"username": normal_user.username})
        resp = admin_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == normal_user.username
        assert "date_joined" in body
        assert "zones_count" in body

    def test_admin_can_patch_email(self, admin_client, normal_user):
        url = reverse("admin-user-detail", kwargs={"username": normal_user.username})
        resp = admin_client.patch(url, {"email": "newmail@example.com"}, format="json")
        assert resp.status_code == 200
        assert resp.json()["email"] == "newmail@example.com"
        normal_user.refresh_from_db()
        assert normal_user.email == "newmail@example.com"

    def test_admin_cannot_set_duplicate_email(
        self, admin_client, normal_user, other_user
    ):
        url = reverse("admin-user-detail", kwargs={"username": normal_user.username})
        resp = admin_client.patch(url, {"email": other_user.email}, format="json")
        assert resp.status_code == 400
        assert "email" in resp.json()

    def test_admin_cannot_change_username(self, admin_client, normal_user):
        url = reverse("admin-user-detail", kwargs={"username": normal_user.username})
        resp = admin_client.patch(url, {"username": "renamed"}, format="json")
        # PATCH succeeds (read-only field ignored) but username stays
        assert resp.status_code == 200
        normal_user.refresh_from_db()
        assert normal_user.username != "renamed"

    def test_invalid_latitude_is_400(self, admin_client, normal_user):
        url = reverse("admin-user-detail", kwargs={"username": normal_user.username})
        resp = admin_client.patch(url, {"latitude": -200}, format="json")
        assert resp.status_code == 400

    def test_delete_soft_deletes(self, admin_client, normal_user):
        url = reverse("admin-user-detail", kwargs={"username": normal_user.username})
        resp = admin_client.delete(url)
        assert resp.status_code == 204
        normal_user.refresh_from_db()
        assert normal_user.is_active is False

    def test_admin_cannot_delete_self(self, admin_client, admin_user):
        url = reverse("admin-user-detail", kwargs={"username": admin_user.username})
        resp = admin_client.delete(url)
        assert resp.status_code == 400

    def test_not_found_returns_404(self, admin_client):
        url = reverse("admin-user-detail", kwargs={"username": "ghost"})
        resp = admin_client.get(url)
        assert resp.status_code == 404
