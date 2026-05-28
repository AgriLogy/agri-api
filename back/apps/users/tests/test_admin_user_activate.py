"""Tests for POST /auth/admin/users/<username>/activate/."""

import pytest
from django.urls import reverse


@pytest.mark.django_db
class TestAdminUserActivate:
    def test_anonymous_is_401(self, anon_client, normal_user):
        url = reverse("admin-user-activate", kwargs={"username": normal_user.username})
        resp = anon_client.post(url)
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_client, other_user):
        url = reverse("admin-user-activate", kwargs={"username": other_user.username})
        resp = user_client.post(url)
        assert resp.status_code == 403

    def test_toggle_without_body_flips_state(self, admin_client, normal_user):
        url = reverse("admin-user-activate", kwargs={"username": normal_user.username})
        assert normal_user.is_active is True

        resp = admin_client.post(url)
        assert resp.status_code == 200
        normal_user.refresh_from_db()
        assert normal_user.is_active is False

        resp = admin_client.post(url)
        assert resp.status_code == 200
        normal_user.refresh_from_db()
        assert normal_user.is_active is True

    def test_explicit_body_sets_state(self, admin_client, normal_user):
        url = reverse("admin-user-activate", kwargs={"username": normal_user.username})
        resp = admin_client.post(url, {"is_active": False}, format="json")
        assert resp.status_code == 200
        normal_user.refresh_from_db()
        assert normal_user.is_active is False

    def test_explicit_body_must_be_bool(self, admin_client, normal_user):
        url = reverse("admin-user-activate", kwargs={"username": normal_user.username})
        resp = admin_client.post(url, {"is_active": "no"}, format="json")
        assert resp.status_code == 400

    def test_admin_cannot_deactivate_self(self, admin_client, admin_user):
        url = reverse("admin-user-activate", kwargs={"username": admin_user.username})
        resp = admin_client.post(url, {"is_active": False}, format="json")
        assert resp.status_code == 400

    def test_not_found_returns_404(self, admin_client):
        url = reverse("admin-user-activate", kwargs={"username": "ghost"})
        resp = admin_client.post(url)
        assert resp.status_code == 404
