"""Tests for the generic admin database CRUD router (/admin/db/*)."""

import pytest

TABLES = "/admin/db/tables"
ZONE = "/admin/db/tables/analytics.zone"


@pytest.mark.django_db
class TestAuth:
    def test_anonymous_is_401(self, anon_client):
        assert anon_client.get(TABLES).status_code == 401

    def test_non_staff_is_403(self, user_bearer):
        assert user_bearer.get(TABLES).status_code == 403


@pytest.mark.django_db
class TestIntrospection:
    def test_list_tables_includes_zone(self, admin_bearer):
        resp = admin_bearer.get(TABLES)
        assert resp.status_code == 200
        keys = {row["key"] for row in resp.json()}
        assert "analytics.zone" in keys
        # Internal bookkeeping tables stay hidden.
        assert not any(k.startswith("contenttypes.") for k in keys)

    def test_schema_reports_fields_and_fk(self, admin_bearer):
        resp = admin_bearer.get(f"{ZONE}/schema")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pk_field"] == "id"
        by_name = {f["name"]: f for f in data["fields"]}
        assert by_name["name"]["type"] == "string"
        assert by_name["user_id"]["type"] == "fk"
        assert by_name["user_id"]["relation"]["to"].endswith("customuser")

    def test_unknown_table_404(self, admin_bearer):
        assert admin_bearer.get("/admin/db/tables/nope.nope/schema").status_code == 404


@pytest.mark.django_db
class TestCrud:
    def test_full_lifecycle(self, admin_bearer, normal_user):
        # CREATE
        payload = {
            "user_id": normal_user.id,
            "name": "Generic Zone",
            "space": 100.0,
            "critical_moisture_threshold": 20.0,
            "pomp_flow_rate": 1.0,
        }
        resp = admin_bearer.post(f"{ZONE}/rows", data=payload, format="json")
        assert resp.status_code == 201, resp.content
        created = resp.json()
        pk = created["__pk__"]
        assert created["name"] == "Generic Zone"

        # LIST + search
        resp = admin_bearer.get(f"{ZONE}/rows", {"search": "Generic"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] >= 1
        assert any(r["__pk__"] == pk for r in body["results"])

        # RETRIEVE
        resp = admin_bearer.get(f"{ZONE}/rows/{pk}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Generic Zone"

        # UPDATE
        resp = admin_bearer.patch(
            f"{ZONE}/rows/{pk}", data={"name": "Renamed"}, format="json"
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

        # DELETE
        resp = admin_bearer.delete(f"{ZONE}/rows/{pk}")
        assert resp.status_code == 204
        assert admin_bearer.get(f"{ZONE}/rows/{pk}").status_code == 404

    def test_create_validation_error_is_400(self, admin_bearer):
        # Missing required FK (user) -> IntegrityError -> 400, not 500.
        resp = admin_bearer.post(f"{ZONE}/rows", data={"name": "x"}, format="json")
        assert resp.status_code == 400
