"""Tests for the manager-affirmation workflow."""

import pytest
from django.urls import reverse

from analytics.models import ManagerAffirmation


@pytest.mark.django_db
class TestManagerAffirmationCreate:
    def test_anonymous_is_401(self, anon_client):
        url = reverse("manager-affirmations")
        resp = anon_client.post(url, {"action": "zone_params_change"}, format="json")
        assert resp.status_code == 401

    def test_user_can_create(self, user_client, normal_user):
        url = reverse("manager-affirmations")
        resp = user_client.post(
            url,
            {"action": "zone_params_change", "payload": {"zone_id": 1}},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert body["status"] == "pending"
        assert body["requested_by_username"] == normal_user.username

    def test_user_sees_only_own(self, user_client, normal_user, other_user):
        ManagerAffirmation.objects.create(
            requested_by=normal_user, action="zone_params_change"
        )
        ManagerAffirmation.objects.create(
            requested_by=other_user, action="zone_params_change"
        )
        url = reverse("manager-affirmations")
        resp = user_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        assert len(rows) == 1
        assert rows[0]["requested_by_username"] == normal_user.username

    def test_admin_sees_all(self, admin_client, normal_user, other_user):
        ManagerAffirmation.objects.create(
            requested_by=normal_user, action="zone_params_change"
        )
        ManagerAffirmation.objects.create(
            requested_by=other_user, action="user_reactivate"
        )
        url = reverse("manager-affirmations")
        resp = admin_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        assert len(rows) == 2

    def test_unknown_action_rejected(self, user_client):
        url = reverse("manager-affirmations")
        resp = user_client.post(url, {"action": "demolish_db"}, format="json")
        assert resp.status_code == 400


@pytest.mark.django_db
class TestManagerAffirmationDecision:
    def _make(self, user):
        return ManagerAffirmation.objects.create(
            requested_by=user, action="zone_params_change"
        )

    def test_admin_approves(self, admin_client, normal_user):
        aff = self._make(normal_user)
        url = reverse(
            "manager-affirmation-decision",
            kwargs={"pk": aff.pk, "action": "approve"},
        )
        resp = admin_client.post(url, {"note": "OK"}, format="json")
        assert resp.status_code == 200
        aff.refresh_from_db()
        assert aff.status == "approved"
        assert aff.decision_note == "OK"
        assert aff.decided_by is not None

    def test_admin_rejects(self, admin_client, normal_user):
        aff = self._make(normal_user)
        url = reverse(
            "manager-affirmation-decision",
            kwargs={"pk": aff.pk, "action": "reject"},
        )
        resp = admin_client.post(url, {"note": "nope"}, format="json")
        assert resp.status_code == 200
        aff.refresh_from_db()
        assert aff.status == "rejected"

    def test_invalid_action_is_400(self, admin_client, normal_user):
        aff = self._make(normal_user)
        url = reverse(
            "manager-affirmation-decision",
            kwargs={"pk": aff.pk, "action": "annihilate"},
        )
        resp = admin_client.post(url)
        assert resp.status_code == 400

    def test_already_decided_is_400(self, admin_client, normal_user):
        aff = self._make(normal_user)
        url = reverse(
            "manager-affirmation-decision",
            kwargs={"pk": aff.pk, "action": "approve"},
        )
        admin_client.post(url, {}, format="json")
        resp = admin_client.post(url, {}, format="json")
        assert resp.status_code == 400

    def test_user_cannot_decide(self, user_client, normal_user):
        aff = self._make(normal_user)
        url = reverse(
            "manager-affirmation-decision",
            kwargs={"pk": aff.pk, "action": "approve"},
        )
        resp = user_client.post(url, {}, format="json")
        assert resp.status_code == 403
