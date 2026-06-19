"""Tests for the admin billing endpoints (django-ninja)."""

import pytest

PLANS_URL = "/admin/billing/plans"
SUBS_URL = "/admin/billing/subscriptions"
INVOICES_URL = "/admin/billing/invoices"


@pytest.mark.django_db
class TestAdminBilling:
    def test_normal_user_is_403(self, user_bearer):
        assert user_bearer.get(PLANS_URL).status_code == 403

    def test_plan_crud(self, admin_bearer):
        resp = admin_bearer.post(
            PLANS_URL,
            {"name": "Pro", "price_dh": 199, "interval": "monthly"},
            format="json",
        )
        assert resp.status_code == 200
        plan_id = resp.json()["id"]
        assert any(p["id"] == plan_id for p in admin_bearer.get(PLANS_URL).json())
        assert admin_bearer.delete(f"{PLANS_URL}/{plan_id}").status_code == 200

    def test_subscription_syncs_payment_status(self, admin_bearer, normal_user):
        plan_id = admin_bearer.post(
            PLANS_URL, {"name": "Basic", "price_dh": 99}, format="json"
        ).json()["id"]
        resp = admin_bearer.post(
            SUBS_URL,
            {"username": normal_user.username, "plan_id": plan_id},
            format="json",
        )
        assert resp.status_code == 200
        sub_id = resp.json()["id"]
        normal_user.refresh_from_db()
        assert normal_user.payement_status == "actif"

        cancel = admin_bearer.post(f"{SUBS_URL}/{sub_id}/cancel", {}, format="json")
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "cancelled"
        normal_user.refresh_from_db()
        assert normal_user.payement_status == "suspended"

    def test_invoice_create_and_mark_paid(self, admin_bearer, normal_user):
        plan_id = admin_bearer.post(
            PLANS_URL, {"name": "P", "price_dh": 10}, format="json"
        ).json()["id"]
        sub_id = admin_bearer.post(
            SUBS_URL,
            {"username": normal_user.username, "plan_id": plan_id},
            format="json",
        ).json()["id"]
        inv = admin_bearer.post(
            INVOICES_URL,
            {"subscription_id": sub_id, "amount_dh": 10},
            format="json",
        )
        assert inv.status_code == 200
        inv_id = inv.json()["id"]
        assert inv.json()["status"] == "unpaid"
        paid = admin_bearer.post(
            f"{INVOICES_URL}/{inv_id}/mark-paid", {}, format="json"
        )
        assert paid.status_code == 200
        assert paid.json()["status"] == "paid"
        assert paid.json()["paid_at"] is not None
