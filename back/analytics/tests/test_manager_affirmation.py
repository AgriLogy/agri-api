"""Tests for the manager-affirmation workflow (django-ninja, JWT bearer auth)."""

import pytest

from analytics.models import ManagerAffirmation

LIST_URL = "/manager-affirmations"


def _approve_url(pk):
    return f"/manager-affirmations/{pk}/approve"


def _reject_url(pk):
    return f"/manager-affirmations/{pk}/reject"


@pytest.mark.django_db
class TestManagerAffirmationCreate:
    def test_anonymous_is_401(self, anon_client):
        resp = anon_client.post(
            LIST_URL, {"action": "zone_params_change"}, format="json"
        )
        assert resp.status_code == 401

    def test_user_can_create(self, user_bearer, normal_user):
        resp = user_bearer.post(
            LIST_URL,
            {"action": "zone_params_change", "payload": {"zone_id": 1}},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert body["status"] == "pending"
        assert body["requested_by_username"] == normal_user.username

    def test_user_sees_only_own(self, user_bearer, normal_user, other_user):
        ManagerAffirmation.objects.create(
            requested_by=normal_user, action="zone_params_change"
        )
        ManagerAffirmation.objects.create(
            requested_by=other_user, action="zone_params_change"
        )
        resp = user_bearer.get(LIST_URL)
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        assert len(rows) == 1
        assert rows[0]["requested_by_username"] == normal_user.username

    def test_admin_sees_all(self, admin_bearer, normal_user, other_user):
        ManagerAffirmation.objects.create(
            requested_by=normal_user, action="zone_params_change"
        )
        ManagerAffirmation.objects.create(
            requested_by=other_user, action="user_reactivate"
        )
        resp = admin_bearer.get(LIST_URL)
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        assert len(rows) == 2

    def test_unknown_action_rejected(self, user_bearer):
        resp = user_bearer.post(LIST_URL, {"action": "demolish_db"}, format="json")
        assert resp.status_code == 400


@pytest.mark.django_db
class TestManagerAffirmationDecision:
    def _make(self, user):
        return ManagerAffirmation.objects.create(
            requested_by=user, action="zone_params_change"
        )

    def test_admin_approves(self, admin_bearer, normal_user):
        aff = self._make(normal_user)
        resp = admin_bearer.post(_approve_url(aff.pk), {"note": "OK"}, format="json")
        assert resp.status_code == 200
        aff.refresh_from_db()
        assert aff.status == "approved"
        assert aff.decision_note == "OK"
        assert aff.decided_by is not None

    def test_admin_rejects(self, admin_bearer, normal_user):
        aff = self._make(normal_user)
        resp = admin_bearer.post(_reject_url(aff.pk), {"note": "nope"}, format="json")
        assert resp.status_code == 200
        aff.refresh_from_db()
        assert aff.status == "rejected"

    def test_invalid_action_is_404(self, admin_bearer, normal_user):
        """The legacy generic decision(action) endpoint is gone; only the
        ``approve`` / ``reject`` paths exist, so an unknown action path 404s."""
        aff = self._make(normal_user)
        resp = admin_bearer.post(
            f"/manager-affirmations/{aff.pk}/annihilate",
            {},
            format="json",
        )
        assert resp.status_code == 404

    def test_already_decided_is_400(self, admin_bearer, normal_user):
        aff = self._make(normal_user)
        admin_bearer.post(_approve_url(aff.pk), {}, format="json")
        resp = admin_bearer.post(_approve_url(aff.pk), {}, format="json")
        assert resp.status_code == 400

    def test_user_cannot_decide(self, user_bearer, normal_user):
        aff = self._make(normal_user)
        resp = user_bearer.post(_approve_url(aff.pk), {}, format="json")
        assert resp.status_code == 403
