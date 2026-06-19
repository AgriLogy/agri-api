"""Tests for the admin system-settings endpoint (django-ninja)."""

import pytest

SETTINGS_URL = "/admin/settings"


@pytest.mark.django_db
class TestAdminSettings:
    def test_normal_user_is_403(self, user_bearer):
        assert user_bearer.get(SETTINGS_URL).status_code == 403

    def test_get_seeds_defaults(self, admin_bearer):
        resp = admin_bearer.get(SETTINGS_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert "notifications" in body
        keys = {row["key"] for rows in body.values() for row in rows}
        assert "support_email" in keys

    def test_patch_upserts(self, admin_bearer):
        admin_bearer.get(SETTINGS_URL)  # seed
        resp = admin_bearer.patch(
            SETTINGS_URL,
            {"values": {"notify_sms_enabled": True, "new_key": "hello"}},
            format="json",
        )
        assert resp.status_code == 200
        flat = {
            row["key"]: row["value"] for rows in resp.json().values() for row in rows
        }
        assert flat["notify_sms_enabled"] is True
        assert flat["new_key"] == "hello"
