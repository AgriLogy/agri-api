"""ActiveGraph.water_level_status — opt-in visibility flag for the water-level
dashboard section (agrilogy-front #4 follow-up)."""

import pytest


@pytest.mark.django_db
class TestActiveGraphWaterLevelStatus:
    def test_default_false_on_new_zone(self, user_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        resp = user_bearer.get(f"/zones/{zone.id}/active-graph")
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert "water_level_status" in body
        assert body["water_level_status"] is False

    def test_admin_can_toggle_on(
        self, admin_bearer, user_bearer, normal_user, zone_factory
    ):
        zone = zone_factory(normal_user)
        url = f"/users/{normal_user.username}/zones/{zone.id}/active-graph"
        resp = admin_bearer.patch(url, {"water_level_status": True}, format="json")
        assert resp.status_code == 200, resp.content
        assert resp.json()["water_level_status"] is True

        # Owner now sees it enabled.
        owner = user_bearer.get(f"/zones/{zone.id}/active-graph").json()
        assert owner["water_level_status"] is True
