"""Tests for admin zone CRUD on the django-ninja API (JWT bearer auth).

New ninja paths (router mounted at root):
  * GET, POST            /users/<username>/zones
  * GET, PUT, PATCH, DELETE  /users/<username>/zones/<pk>
  * GET, PUT, PATCH      /users/<username>/zones/<pk>/params
"""

import pytest


def _zones_url(username):
    return f"/users/{username}/zones"


def _zone_url(username, pk):
    return f"/users/{username}/zones/{pk}"


def _params_url(username, pk):
    return f"/users/{username}/zones/{pk}/params"


@pytest.mark.django_db
class TestAdminZoneList:
    def test_anonymous_is_401(self, anon_client, normal_user):
        resp = anon_client.get(_zones_url(normal_user.username))
        assert resp.status_code == 401

    def test_user_is_403(self, user_bearer, other_user):
        resp = user_bearer.get(_zones_url(other_user.username))
        assert resp.status_code == 403

    def test_admin_lists_only_target_user_zones(
        self, admin_bearer, normal_user, other_user, zone_factory
    ):
        zone_factory(normal_user, name="A")
        zone_factory(other_user, name="B")
        resp = admin_bearer.get(_zones_url(normal_user.username))
        assert resp.status_code == 200
        rows = resp.json()
        names = {r["name"] for r in rows}
        assert "A" in names
        assert "B" not in names

    def test_unknown_user_is_404(self, admin_bearer):
        resp = admin_bearer.get(_zones_url("ghost"))
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

    def test_admin_creates_for_user(self, admin_bearer, normal_user):
        resp = admin_bearer.post(
            _zones_url(normal_user.username),
            self._payload(),
            format="json",
        )
        assert resp.status_code == 201, resp.content
        assert resp.json()["name"] == "north-field"

    def test_user_cannot_create(self, user_bearer, other_user):
        resp = user_bearer.post(
            _zones_url(other_user.username),
            self._payload(),
            format="json",
        )
        assert resp.status_code == 403

    def test_zero_space_rejected(self, admin_bearer, normal_user):
        resp = admin_bearer.post(
            _zones_url(normal_user.username),
            self._payload(space=0),
            format="json",
        )
        assert resp.status_code == 400

    def test_threshold_out_of_range_rejected(self, admin_bearer, normal_user):
        resp = admin_bearer.post(
            _zones_url(normal_user.username),
            self._payload(critical_moisture_threshold=200),
            format="json",
        )
        assert resp.status_code == 400

    def test_negative_flow_rate_rejected(self, admin_bearer, normal_user):
        resp = admin_bearer.post(
            _zones_url(normal_user.username),
            self._payload(pomp_flow_rate=-1),
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestAdminZoneDetail:
    def test_admin_can_patch(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        resp = admin_bearer.patch(
            _zone_url(normal_user.username, zone.pk),
            {"name": "renamed"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed"

    def test_admin_can_delete(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        resp = admin_bearer.delete(_zone_url(normal_user.username, zone.pk))
        assert resp.status_code == 204

    def test_cannot_reach_other_user_zone(
        self, admin_bearer, normal_user, other_user, zone_factory
    ):
        zone = zone_factory(other_user)
        # Look up via the wrong user — should 404.
        resp = admin_bearer.get(_zone_url(normal_user.username, zone.pk))
        assert resp.status_code == 404


@pytest.mark.django_db
class TestAdminZoneParams:
    def test_admin_patches_params(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        resp = admin_bearer.patch(
            _params_url(normal_user.username, zone.pk),
            {"soil_param_TAW": 75.0, "soil_param_RAW": 30.0},
            format="json",
        )
        assert resp.status_code == 200
        zone.refresh_from_db()
        assert zone.soil_param_TAW == 75.0
        assert zone.soil_param_RAW == 30.0

    def test_fc_below_wp_rejected(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        resp = admin_bearer.patch(
            _params_url(normal_user.username, zone.pk),
            {"soil_param_FC": 10, "soil_param_WP": 50},
            format="json",
        )
        assert resp.status_code == 400
