"""Technician (scoped read-only) flow: owner CRUD + enforcement."""

import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.users.models import CustomUser

URL = "/technicians"


def _bearer(user) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return c


def _create_payload(zone_id, graphs):
    return {
        "username": "plumber1",
        "password": "Str0ng-pw-123",
        "firstname": "Plomb",
        "lastname": "Ier",
        "scope": [{"zone_id": zone_id, "allowed_graphs": graphs}],
    }


@pytest.mark.django_db
class TestTechnicianFlow:
    def test_owner_creates_scoped_technician(
        self, user_bearer, normal_user, zone_factory
    ):
        zone = zone_factory(normal_user, name="Z1")
        r = user_bearer.post(
            URL, _create_payload(zone.id, ["water_flow_status"]), format="json"
        )
        assert r.status_code == 201
        body = r.json()
        assert body["username"] == "plumber1"
        assert body["scope"] == [
            {"zone_id": zone.id, "allowed_graphs": ["water_flow_status"]}
        ]

    def test_cannot_scope_foreign_zone(
        self, user_bearer, other_user, zone_factory
    ):
        foreign = zone_factory(other_user, name="Other")
        r = user_bearer.post(
            URL, _create_payload(foreign.id, ["water_flow_status"]), format="json"
        )
        assert r.status_code == 400

    def test_technician_sees_only_granted_zone(
        self, user_bearer, normal_user, zone_factory
    ):
        z1 = zone_factory(normal_user, name="Granted")
        zone_factory(normal_user, name="Hidden")
        user_bearer.post(
            URL, _create_payload(z1.id, ["water_flow_status"]), format="json"
        )
        tech = CustomUser.objects.get(username="plumber1")
        tech_client = _bearer(tech)

        zones = tech_client.get("/zones").json()
        assert {z["id"] for z in zones} == {z1.id}

    def test_active_graph_is_masked(
        self, user_bearer, normal_user, zone_factory
    ):
        z1 = zone_factory(normal_user, name="Granted")
        user_bearer.post(
            URL, _create_payload(z1.id, ["water_flow_status"]), format="json"
        )
        tech = CustomUser.objects.get(username="plumber1")
        ag = _bearer(tech).get(f"/zones/{z1.id}/active-graph").json()
        # Granted graph stays on; a non-granted one is masked off.
        assert ag["water_flow_status"] is True
        assert ag["et0_status"] is False

    def test_technician_cannot_see_ungranted_zone_graph(
        self, user_bearer, normal_user, zone_factory
    ):
        z1 = zone_factory(normal_user, name="Granted")
        z2 = zone_factory(normal_user, name="Ungranted")
        user_bearer.post(
            URL, _create_payload(z1.id, ["water_flow_status"]), format="json"
        )
        tech = CustomUser.objects.get(username="plumber1")
        r = _bearer(tech).get(f"/zones/{z2.id}/active-graph")
        assert r.status_code == 404

    def test_technician_cannot_create_alert(
        self, user_bearer, normal_user, zone_factory
    ):
        z1 = zone_factory(normal_user, name="Granted")
        user_bearer.post(
            URL, _create_payload(z1.id, ["water_flow_status"]), format="json"
        )
        tech = CustomUser.objects.get(username="plumber1")
        r = _bearer(tech).post(
            "/alerts",
            {
                "name": "x",
                "condition": ">",
                "condition_nbr": 1,
                "sensor_key": "water_flow",
            },
            format="json",
        )
        assert r.status_code == 403

    def test_technician_cannot_manage_technicians(
        self, user_bearer, normal_user, zone_factory
    ):
        z1 = zone_factory(normal_user, name="Granted")
        user_bearer.post(
            URL, _create_payload(z1.id, ["water_flow_status"]), format="json"
        )
        tech = CustomUser.objects.get(username="plumber1")
        assert _bearer(tech).get(URL).status_code == 403

    def test_revoke_hides_everything(
        self, user_bearer, normal_user, zone_factory
    ):
        z1 = zone_factory(normal_user, name="Granted")
        created = user_bearer.post(
            URL, _create_payload(z1.id, ["water_flow_status"]), format="json"
        ).json()
        tech = CustomUser.objects.get(username="plumber1")
        assert user_bearer.delete(f"{URL}/{created['id']}").status_code == 200
        # Revoked grant → no visible zones.
        zones = _bearer(tech).get("/zones").json()
        assert zones == []
