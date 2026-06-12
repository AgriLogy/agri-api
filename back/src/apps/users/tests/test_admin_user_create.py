"""Tests for POST /users (admin create, django-ninja, JWT bearer auth)."""

import pytest

URL = "/users"

BASE_PAYLOAD = {
    "username": "newfarmer",
    "email": "newfarmer@example.com",
    "firstname": "New",
    "lastname": "Farmer",
    "phone_number": "+212600000000",
    "password": "Create-Pass-2026!",
}


@pytest.mark.django_db
class TestAdminUserCreate:
    def test_anonymous_is_401(self, anon_client):
        resp = anon_client.post(URL, BASE_PAYLOAD, format="json")
        assert resp.status_code == 401

    def test_normal_user_is_403(self, user_bearer):
        resp = user_bearer.post(URL, BASE_PAYLOAD, format="json")
        assert resp.status_code == 403

    def test_create_without_payement_status_defaults_to_actif(self, admin_bearer):
        """Regression: payement_status is optional in the schema but NOT NULL in
        the DB (model default 'actif'). Omitting it must not 500."""
        resp = admin_bearer.post(URL, BASE_PAYLOAD, format="json")
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert body["username"] == "newfarmer"
        assert body["payement_status"] == "actif"

    def test_create_with_explicit_payement_status(self, admin_bearer):
        payload = {**BASE_PAYLOAD, "payement_status": "suspended"}
        resp = admin_bearer.post(URL, payload, format="json")
        assert resp.status_code == 201, resp.content
        assert resp.json()["payement_status"] == "suspended"
