"""Tests for admin zone CRUD: /api/admin/users/<u>/zones/* ."""

import pytest
from django.urls import reverse


@pytest.mark.django_db
class TestAdminZoneList:
    def test_anonymous_is_401(self, anon_client, normal_user):
        url = reverse("admin-user-zones", kwargs={"username": normal_user.username})
        resp = anon_client.get(url)
        assert resp.status_code == 401

    def test_user_is_403(self, user_client, other_user):
        url = reverse("admin-user-zones", kwargs={"username": other_user.username})
        resp = user_client.get(url)
        assert resp.status_code == 403

    def test_admin_lists_only_target_user_zones(
        self, admin_client, normal_user, other_user, zone_factory
    ):
        zone_factory(normal_user, name="A")
        zone_factory(other_user, name="B")
        url = reverse("admin-user-zones", kwargs={"username": normal_user.username})
        resp = admin_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        names = {r["name"] for r in rows}
        assert "A" in names
        assert "B" not in names

    def test_unknown_user_is_404(self, admin_client):
        url = reverse("admin-user-zones", kwargs={"username": "ghost"})
        resp = admin_client.get(url)
        assert resp.status_code == 404


@pytest.mark.django_db
class TestAdminZoneCreate:
    def _payload(self, **overrides):
        base = {
            "name": "north-field",
            "space": 2500.0,
            "critical_moisture_threshold": 25.0,
            "pomp_flow_rate": 1.5,
        }
        base.update(overrides)
        return base

    def test_admin_creates_for_user(self, admin_client, normal_user):
        url = reverse("admin-user-zones", kwargs={"username": normal_user.username})
        resp = admin_client.post(url, self._payload(), format="json")
        assert resp.status_code == 201, resp.content
        assert resp.json()["name"] == "north-field"

    def test_user_cannot_create(self, user_client, other_user):
        url = reverse("admin-user-zones", kwargs={"username": other_user.username})
        resp = user_client.post(url, self._payload(), format="json")
        assert resp.status_code == 403

    def test_zero_space_rejected(self, admin_client, normal_user):
        url = reverse("admin-user-zones", kwargs={"username": normal_user.username})
        resp = admin_client.post(url, self._payload(space=0), format="json")
        assert resp.status_code == 400

    def test_threshold_out_of_range_rejected(self, admin_client, normal_user):
        url = reverse("admin-user-zones", kwargs={"username": normal_user.username})
        resp = admin_client.post(
            url, self._payload(critical_moisture_threshold=200), format="json"
        )
        assert resp.status_code == 400

    def test_negative_flow_rate_rejected(self, admin_client, normal_user):
        url = reverse("admin-user-zones", kwargs={"username": normal_user.username})
        resp = admin_client.post(url, self._payload(pomp_flow_rate=-1), format="json")
        assert resp.status_code == 400


@pytest.mark.django_db
class TestAdminZoneDetail:
    def test_admin_can_patch(self, admin_client, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        url = reverse(
            "admin-user-zone-detail",
            kwargs={"username": normal_user.username, "pk": zone.pk},
        )
        resp = admin_client.patch(url, {"name": "renamed"}, format="json")
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed"

    def test_admin_can_delete(self, admin_client, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        url = reverse(
            "admin-user-zone-detail",
            kwargs={"username": normal_user.username, "pk": zone.pk},
        )
        resp = admin_client.delete(url)
        assert resp.status_code == 204

    def test_cannot_reach_other_user_zone(
        self, admin_client, normal_user, other_user, zone_factory
    ):
        zone = zone_factory(other_user)
        # Look up via the wrong user — should 404.
        url = reverse(
            "admin-user-zone-detail",
            kwargs={"username": normal_user.username, "pk": zone.pk},
        )
        resp = admin_client.get(url)
        assert resp.status_code == 404


@pytest.mark.django_db
class TestAdminZoneParams:
    def test_admin_patches_params(self, admin_client, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        url = reverse(
            "admin-user-zone-params",
            kwargs={"username": normal_user.username, "pk": zone.pk},
        )
        resp = admin_client.patch(
            url, {"soil_param_TAW": 75.0, "soil_param_RAW": 30.0}, format="json"
        )
        assert resp.status_code == 200
        zone.refresh_from_db()
        assert zone.soil_param_TAW == 75.0
        assert zone.soil_param_RAW == 30.0

    def test_fc_below_wp_rejected(self, admin_client, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        url = reverse(
            "admin-user-zone-params",
            kwargs={"username": normal_user.username, "pk": zone.pk},
        )
        resp = admin_client.patch(
            url, {"soil_param_FC": 10, "soil_param_WP": 50}, format="json"
        )
        assert resp.status_code == 400
