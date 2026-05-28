"""Tests for GET/POST /auth/admin/users/."""

import pytest
from django.urls import reverse


@pytest.mark.django_db
class TestAdminUserList:
    def test_anonymous_is_401(self, anon_client):
        url = reverse("admin-user-list")
        resp = anon_client.get(url)
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_client):
        url = reverse("admin-user-list")
        resp = user_client.get(url)
        assert resp.status_code == 403

    def test_admin_lists_all_users_except_self(
        self, admin_client, admin_user, normal_user, other_user
    ):
        url = reverse("admin-user-list")
        resp = admin_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        usernames = {r["username"] for r in rows}
        assert normal_user.username in usernames
        assert other_user.username in usernames
        assert admin_user.username not in usernames

    def test_admin_list_shape_contains_required_fields(self, admin_client, normal_user):
        url = reverse("admin-user-list")
        resp = admin_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        row = next(r for r in rows if r["username"] == normal_user.username)
        for key in (
            "id",
            "username",
            "email",
            "firstname",
            "lastname",
            "is_active",
            "is_staff",
            "payement_status",
            "zones_count",
            "date_joined",
        ):
            assert key in row, f"missing {key}"

    def test_admin_list_search_by_username(self, admin_client, normal_user, other_user):
        url = reverse("admin-user-list") + f"?search={normal_user.username}"
        resp = admin_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        usernames = {r["username"] for r in rows}
        assert normal_user.username in usernames
        assert other_user.username not in usernames


@pytest.mark.django_db
class TestAdminUserCreate:
    def _payload(self, **overrides):
        base = {
            "username": "fresh",
            "email": "fresh@example.com",
            "firstname": "Fresh",
            "lastname": "Hire",
            "phone_number": "0123456789",
            "password": "Sup3rS3cret!pw",
            "is_staff": False,
            "payement_status": "actif",
        }
        base.update(overrides)
        return base

    def test_anonymous_is_401(self, anon_client):
        url = reverse("admin-user-list")
        resp = anon_client.post(url, self._payload(), format="json")
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_client):
        url = reverse("admin-user-list")
        resp = user_client.post(url, self._payload(), format="json")
        assert resp.status_code == 403

    def test_admin_creates_user(self, admin_client):
        url = reverse("admin-user-list")
        resp = admin_client.post(url, self._payload(), format="json")
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert body["username"] == "fresh"
        assert "password" not in body  # never echoed back

    def test_duplicate_username_is_400(self, admin_client, normal_user):
        url = reverse("admin-user-list")
        resp = admin_client.post(
            url, self._payload(username=normal_user.username), format="json"
        )
        assert resp.status_code == 400
        assert "username" in resp.json()

    def test_duplicate_email_is_400(self, admin_client, normal_user):
        url = reverse("admin-user-list")
        resp = admin_client.post(
            url, self._payload(email=normal_user.email), format="json"
        )
        assert resp.status_code == 400
        assert "email" in resp.json()

    def test_weak_password_is_400(self, admin_client):
        url = reverse("admin-user-list")
        resp = admin_client.post(url, self._payload(password="123"), format="json")
        assert resp.status_code == 400
        assert "password" in resp.json()

    def test_invalid_latitude_is_400(self, admin_client):
        url = reverse("admin-user-list")
        resp = admin_client.post(url, self._payload(latitude=999), format="json")
        assert resp.status_code == 400
        assert "latitude" in resp.json()
