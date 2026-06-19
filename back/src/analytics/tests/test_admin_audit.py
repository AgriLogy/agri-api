"""Tests for the admin audit log endpoint (django-ninja)."""

import pytest

AUDIT_URL = "/admin/audit"
PLANS_URL = "/admin/billing/plans"


@pytest.mark.django_db
class TestAdminAudit:
    def test_normal_user_is_403(self, user_bearer):
        assert user_bearer.get(AUDIT_URL).status_code == 403

    def test_admin_mutation_is_recorded(self, admin_bearer):
        # A billing mutation records an audit event.
        admin_bearer.post(PLANS_URL, {"name": "Audited", "price_dh": 1}, format="json")
        resp = admin_bearer.get(AUDIT_URL)
        assert resp.status_code == 200
        actions = {e["action"] for e in resp.json()}
        assert "plan.create" in actions

    def test_action_filter(self, admin_bearer):
        admin_bearer.post(PLANS_URL, {"name": "X", "price_dh": 1}, format="json")
        resp = admin_bearer.get(f"{AUDIT_URL}?action=plan.create")
        assert resp.status_code == 200
        assert all("plan.create" in e["action"] for e in resp.json())
