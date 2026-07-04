"""F6-admin-db golden parity: the generic ``/admin/db/*`` schema-driven CRUD.

This is the hardest strangler port — the Django version (``agriapi/api/
router_db.py``) introspects **Django models**; the fastapp version introspects
the **agri.db SQLAlchemy metadata**. Full byte-parity on the introspection
responses (``/tables`` + ``/schema``) is *not* attainable, because Django's
``verbose_name`` / ``help_text`` / ``choices`` / per-field ``required`` /
``editable`` and the field *ordering* all come from Django model metadata that
has no counterpart in the DB schema. So this suite asserts byte-parity on what
IS well-defined and is what the frontend keys off:

  * the table ``key`` format (``app_label.modelname``, incl. ``CustomUser.``),
    ``app_label``, ``model_name``, ``pk_field``;
  * the *set* of field names + each field's ``type`` / ``primary_key`` /
    ``nullable`` and the FK ``relation.to`` target;
  * the ``{"detail": ...}`` error envelopes (unknown table / row not found),
    which ARE byte-identical to the Django endpoint;
  * the row-CRUD lifecycle (create → refetch → update → delete → 404) with the
    Django JSON coercion (dates → ISO, Decimal → float).

TABLE-SET DELTA vs Django (documented, asserted below):
  * fastapp-only: ``analytics.devicesensor`` — the table exists in the agri.db
    schema-of-record but the Django DeviceSensor model isn't in this repo yet.
  * Django-only: ``auth.group``, ``auth.permission`` and the six
    ``django_celery_beat.*`` tables — Django-runtime apps not mirrored in
    agri.db. Everything else is a shared key with a byte-identical db_table.
  * The two auto-created M2M through tables
    (``CustomUser_customuser_{groups,user_permissions}``) are hidden on both
    surfaces (Django's ``get_models()`` excludes them).

Dual-ORM: Postgres only + committed rows (fastapp reads via a separate
SQLAlchemy connection). Runs alongside test_adminbiz_parity.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.routers import admin_db
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture(autouse=True)
def _clean_zone_rows(db):
    """analytics_zone is managed (migrations), so TransactionTestCase truncates
    it — but be defensive and clear it around each test so both surfaces see
    only this test's rows."""

    def _wipe():
        from apps.irrigation.models import Zone

        Zone.objects.all().delete()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def django() -> APIClient:
    return APIClient()


@pytest.fixture
def admin(django_user_model):
    return django_user_model.objects.create_user(
        username="db-admin",
        email="db-admin@example.com",
        password="irrelevant-3921",
        is_staff=True,
    )


@pytest.fixture
def plain(django_user_model):
    return django_user_model.objects.create_user(
        username="db-plain",
        email="db-plain@example.com",
        password="irrelevant-3921",
    )


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {_token(user)}"}


def _both(fast, django, user, path, method="get", **kw):
    tok = _token(user)
    dj = getattr(django, method)(path, HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = getattr(fast, method)(path, headers={"Authorization": f"Bearer {tok}"})
    return dj, fp


# ===========================================================================
# non-staff 403 (status is the contract; the 403 body differs by design, same
# as the other admin routers)
# ===========================================================================
_STAFF_ROUTES = [
    "/admin/db/tables",
    "/admin/db/tables/analytics.zone/schema",
    "/admin/db/tables/analytics.zone/rows",
    "/admin/db/tables/analytics.zone/rows/1",
]


@pytest.mark.parametrize("path", _STAFF_ROUTES)
def test_non_staff_is_403(fast, plain, path):
    r = fast.get(path, headers=_auth(plain))
    assert r.status_code == 403


def test_unauthenticated_is_401(fast):
    r = fast.get("/admin/db/tables")
    assert r.status_code == 401


# ===========================================================================
# table list — the KEY set is the crux the frontend depends on
# ===========================================================================
def _django_visible_keys() -> set[str]:
    from agriapi.api.router_db import _visible_models

    return {m._meta.label_lower for m in _visible_models()}


def test_tables_list_keys_are_a_clean_subset(fast, admin):
    r = fast.get("/admin/db/tables", headers=_auth(admin))
    assert r.status_code == 200
    body = r.json()
    fast_keys = {row["key"] for row in body}
    dj_keys = _django_visible_keys()

    # Every fastapp key is byte-identical to a Django label_lower EXCEPT the one
    # documented fastapp-only extra (agri.db has the table; Django has no model).
    assert fast_keys - dj_keys == {"analytics.devicesensor"}

    # The documented Django-only extras are absent from fastapp (not in agri.db).
    django_only = dj_keys - fast_keys
    assert django_only == {
        "auth.group",
        "auth.permission",
        "django_celery_beat.clockedschedule",
        "django_celery_beat.crontabschedule",
        "django_celery_beat.intervalschedule",
        "django_celery_beat.periodictask",
        "django_celery_beat.periodictasks",
        "django_celery_beat.solarschedule",
    }

    # The auto-created M2M through tables are hidden.
    assert "CustomUser.customuser_groups" not in fast_keys
    assert "CustomUser.customuser_user_permissions" not in fast_keys

    # The user table keeps Django's mixed-case app_label in the key.
    assert "CustomUser.customuser" in fast_keys


def test_tables_list_shape_and_app_label(fast, admin):
    r = fast.get("/admin/db/tables", headers=_auth(admin))
    by_key = {row["key"]: row for row in r.json()}

    zone = by_key["analytics.zone"]
    assert set(zone) == {
        "key",
        "app_label",
        "model_name",
        "verbose_name",
        "verbose_name_plural",
        "count",
    }
    assert zone["app_label"] == "analytics"
    assert zone["model_name"] == "zone"

    user_row = by_key["CustomUser.customuser"]
    assert user_row["app_label"] == "CustomUser"
    assert user_row["model_name"] == "customuser"

    # Rows are ordered by key, exactly like Django's sorted(label_lower).
    keys = [row["key"] for row in r.json()]
    assert keys == sorted(keys)


def test_tables_count_reflects_committed_rows(fast, admin, plain):
    _seed_zone(plain, name="counted")
    r = fast.get("/admin/db/tables", headers=_auth(admin))
    by_key = {row["key"]: row for row in r.json()}
    assert by_key["analytics.zone"]["count"] == 1


# ===========================================================================
# schema — byte-parity on the derivable projection vs Django's own _schema
# ===========================================================================
# Tables whose SQLAlchemy mirror agrees field-for-field with the Django model on
# the derivable projection (name/type/pk/nullable/fk-target).
_SCHEMA_TABLES = [
    "analytics.zone",
    "analytics.soilmoisturehigh",
    "CustomUser.customuser",
    "analytics.alert",
    "analytics.kc",
]

# Tables where the agri.db mirror (source = the LIVE DB) legitimately diverges
# from Django's declared model metadata. Each entry maps a field name to the
# projection keys that differ — everything else must still match. Documented so
# the drift is pinned, not silently dropped:
#   * assistant.assistantconversation.user_id — the SA mirror declares user_id as
#     a plain BigInteger (the assistant tables were absorbed from ensure_* boot
#     scripts WITHOUT the FK constraint), so fastapp sees "integer" where Django's
#     ForeignKey sees "fk"/relation.
#   * feedback.bugreport.video_url — nullable=True in the real DB column, but the
#     Django model declares null=False; fastapp reflects the actual schema.
_SCHEMA_KNOWN_DELTAS = {
    "assistant.assistantconversation": {"user_id"},
    "feedback.bugreport": {"video_url"},
}


def _field_projection(fields: list[dict]) -> dict[str, dict]:
    """The subset of each field descriptor that CAN match across the two
    introspection sources (name/type/pk/nullable + FK target)."""
    out = {}
    for f in fields:
        proj = {
            "type": f["type"],
            "primary_key": f["primary_key"],
            "nullable": f["nullable"],
        }
        if "relation" in f:
            proj["relation_to"] = f["relation"]["to"]
        out[f["name"]] = proj
    return out


@pytest.mark.parametrize("key", _SCHEMA_TABLES)
def test_schema_derivable_projection_matches_django(fast, admin, key):
    from agriapi.api.router_db import _resolve_model as dj_resolve
    from agriapi.api.router_db import _schema as dj_schema

    dj = dj_schema(dj_resolve(key))
    fp = fast.get(f"/admin/db/tables/{key}/schema", headers=_auth(admin)).json()

    # Top-level scalars byte-match.
    for attr in ("key", "app_label", "model_name", "pk_field"):
        assert fp[attr] == dj[attr], (attr, fp[attr], dj[attr])

    # Same set of field names (column mirror), same type/pk/nullable/fk-target.
    assert _field_projection(fp["fields"]) == _field_projection(dj["fields"])


@pytest.mark.parametrize("key", sorted(_SCHEMA_KNOWN_DELTAS))
def test_schema_known_deltas_are_exactly_documented(fast, admin, key):
    """The two tables where the SA mirror diverges from the Django model: assert
    the difference is EXACTLY the documented field(s), and every other field's
    projection still matches byte-for-byte."""
    from agriapi.api.router_db import _resolve_model as dj_resolve
    from agriapi.api.router_db import _schema as dj_schema

    dj = _field_projection(dj_schema(dj_resolve(key))["fields"])
    fp = _field_projection(
        fast.get(f"/admin/db/tables/{key}/schema", headers=_auth(admin)).json()[
            "fields"
        ]
    )
    assert set(dj) == set(fp)  # same field-name set
    differing = {name for name in dj if dj[name] != fp[name]}
    assert differing == _SCHEMA_KNOWN_DELTAS[key]


def test_schema_unknown_table_404_byte_identical(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/admin/db/tables/nope.nada/schema")
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content
    assert fp.json() == {"detail": "Unknown table 'nope.nada'."}


# ===========================================================================
# rows — list envelope, CRUD lifecycle, error envelopes
# ===========================================================================
def _zone_body(user, **over):
    body = {
        "user_id": user.id,
        "name": "z-crud",
        "space": 1000.0,
        "critical_moisture_threshold": 20.0,
        "irrigation_water_quantity": 100.0,
        "pomp_flow_rate": 1.0,
        "soil_param_FC": 50.0,
        "soil_param_RAW": 50.0,
        "soil_param_TAW": 50.0,
        "soil_param_WP": 50.0,
        "elevation_m": 120.0,
    }
    body.update(over)
    return body


def _seed_zone(user, **over):
    from apps.irrigation.models import Zone

    payload = _zone_body(user, **over)
    payload.pop("user_id")
    return Zone.objects.create(user=user, **payload)


def test_rows_list_envelope_and_search(fast, admin, plain):
    _seed_zone(plain, name="alpha")
    _seed_zone(plain, name="beta")
    r = fast.get("/admin/db/tables/analytics.zone/rows", headers=_auth(admin))
    body = r.json()
    assert set(body) == {"count", "page", "page_size", "results"}
    assert body["count"] == 2
    assert body["page"] == 1 and body["page_size"] == admin_db._DEFAULT_PAGE_SIZE
    # each row carries the pk handle + a __str__ label
    assert all("__pk__" in row and "__str__" in row for row in body["results"])

    # search over text columns (name __icontains)
    r = fast.get(
        "/admin/db/tables/analytics.zone/rows?search=alph", headers=_auth(admin)
    )
    body = r.json()
    assert body["count"] == 1
    assert body["results"][0]["name"] == "alpha"

    # pagination
    r = fast.get(
        "/admin/db/tables/analytics.zone/rows?page=1&page_size=1",
        headers=_auth(admin),
    )
    assert r.json()["page_size"] == 1
    assert len(r.json()["results"]) == 1


def test_rows_crud_lifecycle(fast, admin, plain):
    from apps.irrigation.models import Zone

    tok = _auth(admin)
    # CREATE
    r = fast.post(
        "/admin/db/tables/analytics.zone/rows",
        json=_zone_body(plain, name="created"),
        headers=tok,
    )
    assert r.status_code == 201, r.text
    created = r.json()
    pk = created["__pk__"]
    assert created["name"] == "created"
    assert created["space"] == 1000.0
    assert Zone.objects.filter(id=pk, name="created").exists()

    # RETRIEVE (refetch)
    r = fast.get(f"/admin/db/tables/analytics.zone/rows/{pk}", headers=tok)
    assert r.status_code == 200
    assert r.json()["name"] == "created"

    # UPDATE (partial)
    r = fast.patch(
        f"/admin/db/tables/analytics.zone/rows/{pk}",
        json={"name": "renamed"},
        headers=tok,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "renamed"
    Zone.objects.get(id=pk)  # still there
    assert Zone.objects.get(id=pk).name == "renamed"

    # DELETE
    r = fast.delete(f"/admin/db/tables/analytics.zone/rows/{pk}", headers=tok)
    assert r.status_code == 204
    assert not Zone.objects.filter(id=pk).exists()

    # RETRIEVE after delete → 404
    r = fast.get(f"/admin/db/tables/analytics.zone/rows/{pk}", headers=tok)
    assert r.status_code == 404
    assert r.json() == {"detail": "Row not found."}


def test_row_not_found_404_byte_identical(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/admin/db/tables/analytics.zone/rows/99999999")
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content
    assert fp.json() == {"detail": "Row not found."}


def test_rows_unknown_table_404(fast, admin):
    r = fast.get("/admin/db/tables/nope.nada/rows", headers=_auth(admin))
    assert r.status_code == 404
    assert r.json() == {"detail": "Unknown table 'nope.nada'."}
