"""Tests for the 7-day ET0 forecast endpoint (GET /weather/et-forecast)."""

import pytest

URL = "/weather/et-forecast"


@pytest.mark.django_db
class TestEtForecast:
    def test_requires_auth(self, anon_client, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        resp = anon_client.get(f"{URL}?zone_id={zone.id}")
        assert resp.status_code == 401

    def test_returns_seven_days_for_owner(self, user_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        resp = user_bearer.get(f"{URL}?zone_id={zone.id}")
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["zone_id"] == zone.id
        assert body["provider"] == "mock"
        assert len(body["days"]) == 7
        for day in body["days"]:
            assert "date" in day
            assert isinstance(day["et0_mm"], (int, float))
            assert day["et0_mm"] >= 0.0

    def test_days_param_clamped(self, user_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        assert (
            len(user_bearer.get(f"{URL}?zone_id={zone.id}&days=3").json()["days"]) == 3
        )
        # clamp upper bound (max 14)
        assert (
            len(user_bearer.get(f"{URL}?zone_id={zone.id}&days=99").json()["days"])
            == 14
        )

    def test_deterministic(self, user_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        a = user_bearer.get(f"{URL}?zone_id={zone.id}").json()["days"]
        b = user_bearer.get(f"{URL}?zone_id={zone.id}").json()["days"]
        assert a == b

    def test_other_users_zone_is_404(
        self, user_bearer, normal_user, other_user, zone_factory
    ):
        other_zone = zone_factory(other_user)
        resp = user_bearer.get(f"{URL}?zone_id={other_zone.id}")
        assert resp.status_code == 404
