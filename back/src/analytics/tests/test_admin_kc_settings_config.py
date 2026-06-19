"""Tests for Wave 2D admin endpoints: global Kc CRUD, SystemSetting
create/delete, and per-user GraphName / SensorColor config."""

import pytest

KC_URL = "/admin/kc"
SETTINGS_URL = "/admin/settings"


@pytest.mark.django_db
class TestAdminKc:
    def test_normal_user_is_403(self, user_bearer):
        assert user_bearer.get(KC_URL).status_code == 403

    def test_create_list_delete(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user, name="z1")
        resp = admin_bearer.post(
            KC_URL,
            {
                "username": normal_user.username,
                "name": "Tomato",
                "plant_name": "tomato",
                "zone_id": zone.pk,
                "periods": [
                    {
                        "period_name": "init",
                        "start_date": "2026-01-01",
                        "end_date": "2026-02-01",
                        "kc_value": 0.6,
                    }
                ],
            },
            format="json",
        )
        assert resp.status_code == 201, resp.content
        kc_id = resp.json()["id"]
        assert resp.json()["username"] == normal_user.username
        assert len(resp.json()["periods"]) == 1

        listed = admin_bearer.get(KC_URL).json()
        assert any(k["id"] == kc_id for k in listed)

        # filter by username
        filtered = admin_bearer.get(f"{KC_URL}?username={normal_user.username}").json()
        assert all(k["username"] == normal_user.username for k in filtered)

        assert admin_bearer.delete(f"{KC_URL}/{kc_id}").status_code == 200
        assert all(k["id"] != kc_id for k in admin_bearer.get(KC_URL).json())

    def test_create_unknown_user_404(self, admin_bearer):
        resp = admin_bearer.post(
            KC_URL,
            {"username": "ghost", "name": "x", "plant_name": "x"},
            format="json",
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestAdminSettings:
    def test_create_and_delete_key(self, admin_bearer):
        resp = admin_bearer.post(
            SETTINGS_URL,
            {"key": "custom_flag", "value": True, "category": "experimental"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        grouped = resp.json()
        assert any(s["key"] == "custom_flag" for s in grouped.get("experimental", []))

        # duplicate → 409
        dup = admin_bearer.post(
            SETTINGS_URL, {"key": "custom_flag", "value": False}, format="json"
        )
        assert dup.status_code == 409

        assert admin_bearer.delete(f"{SETTINGS_URL}/custom_flag").status_code == 200
        assert admin_bearer.delete(f"{SETTINGS_URL}/custom_flag").status_code == 404

    def test_normal_user_is_403(self, user_bearer):
        assert (
            user_bearer.post(SETTINGS_URL, {"key": "x"}, format="json").status_code
            == 403
        )


@pytest.mark.django_db
class TestAdminPerUserConfig:
    def _gn_url(self, u, z):
        return f"/users/{u}/zones/{z}/graph-names"

    def _sc_url(self, u, z):
        return f"/users/{u}/zones/{z}/sensor-colors"

    def test_graph_names_get_and_patch(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        got = admin_bearer.get(self._gn_url(normal_user.username, zone.pk))
        assert got.status_code == 200
        patched = admin_bearer.patch(
            self._gn_url(normal_user.username, zone.pk),
            {"soil_irrigation": "Renamed label"},
            format="json",
        )
        assert patched.status_code == 200
        assert patched.json()["soil_irrigation"] == "Renamed label"

    def test_sensor_colors_patch(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user)
        patched = admin_bearer.patch(
            self._sc_url(normal_user.username, zone.pk),
            {"et0_color": "#123456"},
            format="json",
        )
        assert patched.status_code == 200
        assert patched.json()["et0_color"] == "#123456"

    def test_config_user_is_403(self, user_bearer, other_user, zone_factory):
        zone = zone_factory(other_user)
        assert (
            user_bearer.get(self._gn_url(other_user.username, zone.pk)).status_code
            == 403
        )
