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


@pytest.mark.django_db
class TestManagerAffirmationApply:
    """Approving an affirmation applies its payload to the underlying resource."""

    def test_approve_applies_zone_params(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user, soil_param_RAW=10.0, pomp_flow_rate=1.0)
        aff = ManagerAffirmation.objects.create(
            requested_by=normal_user,
            action="zone_params_change",
            payload={
                "zone_id": zone.id,
                "fields": {"soil_param_RAW": 42.0, "pomp_flow_rate": 3.5},
            },
        )
        resp = admin_bearer.post(_approve_url(aff.pk), {}, format="json")
        assert resp.status_code == 200, resp.content
        aff.refresh_from_db()
        zone.refresh_from_db()
        assert aff.status == "approved"
        assert zone.soil_param_RAW == 42.0
        assert zone.pomp_flow_rate == 3.5

    def test_reject_leaves_zone_unchanged(
        self, admin_bearer, normal_user, zone_factory
    ):
        zone = zone_factory(normal_user, soil_param_RAW=10.0)
        aff = ManagerAffirmation.objects.create(
            requested_by=normal_user,
            action="zone_params_change",
            payload={"zone_id": zone.id, "fields": {"soil_param_RAW": 99.0}},
        )
        resp = admin_bearer.post(_reject_url(aff.pk), {}, format="json")
        assert resp.status_code == 200
        aff.refresh_from_db()
        zone.refresh_from_db()
        assert aff.status == "rejected"
        assert zone.soil_param_RAW == 10.0

    def test_invalid_field_is_400_and_stays_pending(
        self, admin_bearer, normal_user, zone_factory
    ):
        zone = zone_factory(normal_user)
        aff = ManagerAffirmation.objects.create(
            requested_by=normal_user,
            action="zone_params_change",
            payload={"zone_id": zone.id, "fields": {"is_active": False}},
        )
        resp = admin_bearer.post(_approve_url(aff.pk), {}, format="json")
        assert resp.status_code == 400
        aff.refresh_from_db()
        assert aff.status == "pending"

    def test_approve_other_users_zone_is_400(
        self, admin_bearer, normal_user, other_user, zone_factory
    ):
        zone = zone_factory(other_user, soil_param_RAW=10.0)
        aff = ManagerAffirmation.objects.create(
            requested_by=normal_user,
            action="zone_params_change",
            payload={"zone_id": zone.id, "fields": {"soil_param_RAW": 42.0}},
        )
        resp = admin_bearer.post(_approve_url(aff.pk), {}, format="json")
        assert resp.status_code == 400
        zone.refresh_from_db()
        assert zone.soil_param_RAW == 10.0

    def test_approve_replaces_kc_periods(self, admin_bearer, normal_user, zone_factory):
        from apps.irrigation.models import Kc

        zone = zone_factory(normal_user)
        kc = Kc.objects.create(
            name="Tomato",
            plant_name="Tomato",
            user=normal_user,
            zone=zone,
            number_of_periods=0,
        )
        aff = ManagerAffirmation.objects.create(
            requested_by=normal_user,
            action="kc_periods_change",
            payload={
                "kc_id": kc.id,
                "periods": [
                    {
                        "period_name": "Initial",
                        "start_date": "2026-01-01",
                        "end_date": "2026-02-01",
                        "kc_value": 0.6,
                    },
                    {
                        "period_name": "Mid",
                        "start_date": "2026-02-02",
                        "end_date": "2026-03-01",
                        "kc_value": 1.1,
                    },
                ],
            },
        )
        resp = admin_bearer.post(_approve_url(aff.pk), {}, format="json")
        assert resp.status_code == 200, resp.content
        kc.refresh_from_db()
        assert kc.number_of_periods == 2
        assert kc.periods.count() == 2

    def test_approve_reactivates_user(self, admin_bearer, normal_user, other_user):
        other_user.is_active = False
        other_user.save(update_fields=["is_active"])
        aff = ManagerAffirmation.objects.create(
            requested_by=normal_user,
            action="user_reactivate",
            payload={"user_id": other_user.id},
        )
        resp = admin_bearer.post(_approve_url(aff.pk), {}, format="json")
        assert resp.status_code == 200
        other_user.refresh_from_db()
        assert other_user.is_active is True
