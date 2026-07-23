"""Sensor groups + per-sensor calibration (#439) — the API and its schema shim.

``analytics_sensorgroup`` / ``analytics_sensorgroupsensor`` /
``analytics_sensorcalibration`` are created by the agri-db migration
``f4b6d2e8c1a9``, which production has NOT applied. Every endpoint therefore
asks ``fastapp.schema_compat`` whether the tables are there and degrades:
reads answer empty / the identity calibration, writes are refused with a 400
naming the migration. Nothing may ever surface as an ``UndefinedTable`` 500 —
that is the defect class of #432 / #434 / #436.

Two levels:

* unit — the table probe (present / absent, cached once per engine per table)
  plus the routers' pure request/response + ownership helpers: in-memory
  sqlite and stub sessions, no Postgres;
* integration — the REAL endpoints over ``TestClient`` against real Postgres
  schemas built in BOTH shapes (with the tables, and without), parametrized.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import agri.db.analytics  # noqa: F401  (registers every table on AgriBase)
import agri.db.devices  # noqa: F401
import agri.db.irrigation  # noqa: F401
import agri.db.technicians  # noqa: F401
import agri.db.users  # noqa: F401
from agri.db.base import AgriBase
from fastapp import schema_compat
from fastapp.auth import AuthedUser, get_current_user
from fastapp.main import app
from fastapp.routers import sensor_calibration as calib_router
from fastapp.routers import sensor_groups as groups_router
from fastapp.routers.selfreads import _ReadScope

# The three tables the held migration creates, verbatim from agri-db#70
# (revision f4b6d2e8c1a9). Hand-rolled DDL so the "absent" shape is a schema
# genuinely missing genuine tables, not a mocked one.
_SENSOR_GROUP_DDL = """
CREATE TABLE analytics_sensorgroup (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    user_id BIGINT NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT analytics_sensorgroup_user_name_key UNIQUE (user_id, name)
)
"""

_SENSOR_GROUP_SENSOR_DDL = """
CREATE TABLE analytics_sensorgroupsensor (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL
        REFERENCES analytics_sensorgroup (id) ON DELETE CASCADE,
    device_id BIGINT NOT NULL,
    sensor_key VARCHAR(64) NOT NULL,
    label VARCHAR(120) NOT NULL DEFAULT '',
    zone_id BIGINT REFERENCES analytics_zone (id) ON DELETE SET NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT analytics_sensorgroupsensor_key
        UNIQUE (group_id, device_id, sensor_key)
)
"""

_SENSOR_CALIBRATION_DDL = """
CREATE TABLE analytics_sensorcalibration (
    id BIGSERIAL PRIMARY KEY,
    device_id BIGINT NOT NULL,
    sensor_key VARCHAR(64) NOT NULL,
    scale_a DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    offset_b DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    unit VARCHAR(32) NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT true,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT analytics_sensorcalibration_key UNIQUE (device_id, sensor_key)
)
"""

_NEW_TABLE_DDL = (
    _SENSOR_GROUP_DDL,
    _SENSOR_GROUP_SENSOR_DDL,
    _SENSOR_CALIBRATION_DDL,
)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    schema_compat.reset_table_cache()
    yield
    schema_compat.reset_table_cache()


# ---------------------------------------------------------------------------
# Unit level — the probe. No Postgres, no Django DB.
# ---------------------------------------------------------------------------
class _StubBind:
    """A weakref-able stand-in for an Engine (the probe cache keys on it)."""


class _StubSession:
    def __init__(self, bind):
        self._bind = bind

    def get_bind(self):
        return self._bind


def _sqlite_engine(with_tables: bool):
    # default pool (one shared connection): an in-memory sqlite DB lives and
    # dies with its connection, so NullPool would throw the tables away.
    engine = create_engine("sqlite://")
    if with_tables:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE analytics_sensorgroup (id INTEGER)"))
            conn.execute(text("CREATE TABLE analytics_sensorgroupsensor (id INTEGER)"))
            conn.execute(text("CREATE TABLE analytics_sensorcalibration (id INTEGER)"))
    return engine


@pytest.mark.parametrize("with_tables", [False, True], ids=["absent", "present"])
def test_table_probe_reports_the_real_schema_shape(with_tables):
    engine = _sqlite_engine(with_tables)
    session = Session(engine)
    try:
        assert schema_compat.sensor_groups_available(session) is with_tables
        assert schema_compat.sensor_calibration_available(session) is with_tables
    finally:
        session.close()
        engine.dispose()


def test_sensor_groups_need_both_tables():
    # A group table without its membership table is not a usable feature; the
    # probe must say "absent" rather than half-enable the router.
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE analytics_sensorgroup (id INTEGER)"))
    session = Session(engine)
    try:
        assert schema_compat.sensor_groups_available(session) is False
    finally:
        session.close()
        engine.dispose()


def test_table_probe_runs_once_per_engine_and_table(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        schema_compat,
        "_probe_table",
        lambda engine, table: (calls.append((engine, table)), True)[1],
    )
    session = _StubSession(_StubBind())

    results = [schema_compat.sensor_groups_available(session) for _ in range(25)]

    assert results == [True] * 25
    # exactly one probe per table, never one per request
    assert len(calls) == 2
    assert {table for _, table in calls} == {
        schema_compat.SENSOR_GROUP_TABLE,
        schema_compat.SENSOR_GROUP_SENSOR_TABLE,
    }


def test_table_probe_is_cached_per_engine(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        schema_compat,
        "_probe_table",
        lambda engine, table: (calls.append((engine, table)), True)[1],
    )
    first, second = _StubBind(), _StubBind()

    schema_compat.sensor_calibration_available(_StubSession(first))
    schema_compat.sensor_calibration_available(_StubSession(second))
    schema_compat.sensor_calibration_available(_StubSession(first))

    assert [engine for engine, _ in calls] == [first, second]


def test_reset_table_cache_forgets_every_engine(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        schema_compat,
        "_probe_table",
        lambda engine, table: (calls.append((engine, table)), True)[1],
    )
    session = _StubSession(_StubBind())

    schema_compat.sensor_calibration_available(session)
    schema_compat.reset_table_cache()
    schema_compat.sensor_calibration_available(session)

    assert len(calls) == 2


def test_probe_never_raises_on_a_hostile_engine(monkeypatch):
    class _Boom:
        def has_table(self, name):
            raise RuntimeError("inspection refused")

    monkeypatch.setattr(schema_compat, "sa_inspect", lambda engine: _Boom())
    # degrade to "absent" — the whole point is that a schema gap is never a 500
    assert schema_compat.table_available(_StubSession(_StubBind()), "x") is False


def test_unavailable_messages_name_the_migration():
    assert schema_compat.SENSOR_GROUPS_MIGRATION == "f4b6d2e8c1a9"
    for message in (
        schema_compat.SENSOR_GROUPS_UNAVAILABLE,
        schema_compat.SENSOR_CALIBRATION_UNAVAILABLE,
    ):
        assert "f4b6d2e8c1a9" in message


# ---------------------------------------------------------------------------
# Unit level — request/response validation + ownership helpers.
# ---------------------------------------------------------------------------
def test_group_payload_rejects_an_empty_name():
    with pytest.raises(Exception):
        groups_router.SensorGroupIn(name="")


def test_group_payload_rejects_an_over_long_name():
    with pytest.raises(Exception):
        groups_router.SensorGroupIn(name="x" * 121)


def test_group_sensor_payload_caps_the_sensor_key_at_the_column_width():
    groups_router.GroupSensorIn(device_id=1, sensor_key="x" * 64)
    with pytest.raises(Exception):
        groups_router.GroupSensorIn(device_id=1, sensor_key="x" * 65)


def test_calibration_payload_defaults_to_the_identity_transform():
    payload = calib_router.CalibrationIn()
    assert (payload.scale_a, payload.offset_b, payload.unit) == (1.0, 0.0, "")
    assert payload.is_active is True


def test_read_only_scope_is_refused_write_access():
    read_only = _ReadScope(owner_id=7, zone_ids={1}, is_read_only=True)
    blocked = groups_router._block_if_read_only(read_only)
    assert blocked is not None and blocked.status_code == 403

    owner = _ReadScope(owner_id=7, zone_ids=None)
    assert groups_router._block_if_read_only(owner) is None


def _sensor_row(sid: int, zone: int | None):
    return SimpleNamespace(
        id=sid,
        group_id=1,
        device_id=30,
        sensor_key="soil_moisture",
        label="",
        zone_id=zone,
        display_order=0,
        device_name="Sensor A",
        device_serial="S-1",
        effective_zone_id=zone,
    )


def test_membership_listing_hides_sensors_outside_a_technician_scope():
    session = SimpleNamespace(
        execute=lambda stmt, params: [_sensor_row(1, 20), _sensor_row(2, 21)]
    )
    technician = _ReadScope(owner_id=10, zone_ids={20}, is_read_only=True)

    visible = groups_router._sensors_for(session, [1], technician)

    assert [s["id"] for s in visible[1]] == [1]


def test_membership_listing_shows_everything_to_the_owner():
    session = SimpleNamespace(
        execute=lambda stmt, params: [_sensor_row(1, 20), _sensor_row(2, 21)]
    )
    owner = _ReadScope(owner_id=10, zone_ids=None)

    visible = groups_router._sensors_for(session, [1], owner)

    assert [s["id"] for s in visible[1]] == [1, 2]


def test_calibration_default_is_the_identity_transform():
    default = calib_router._default(30, "soil_moisture", available=False)
    assert (default["scale_a"], default["offset_b"]) == (1.0, 0.0)
    assert default["configured"] is False
    assert default["schema_available"] is False


# ---------------------------------------------------------------------------
# Integration level — the real endpoints against real Postgres, in both shapes.
# The tables live in their own schema (search_path) so the Django test database
# is untouched: the routers' unqualified SQL resolves there, exactly as it
# resolves to ``public`` in production.
# ---------------------------------------------------------------------------
_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="the sensor-group endpoints are exercised against a real Postgres "
    "schema (fastapp reads the test DB over SQLAlchemy)",
)

_SCHEMA = {False: "compat_no_sensor_groups", True: "compat_sensor_groups"}

_OWNER_ID = 10
_OTHER_ID = 11
_TECHNICIAN_ID = 12
_ZONE_ID = 20
_OTHER_ZONE_ID = 21
_DEVICE_ID = 30
_OTHER_DEVICE_ID = 31


def _engine_for(schema: str):
    return create_engine(
        os.environ["AGRI_DB_URL"],
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={schema}"},
    )


def _build_schema(has_tables: bool):
    """Create ``<schema>`` with every agri-db table, plus (when requested) the
    three tables migration ``f4b6d2e8c1a9`` adds. Returns the bound engine."""
    schema = _SCHEMA[has_tables]
    admin = create_engine(os.environ["AGRI_DB_URL"], poolclass=NullPool)
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    admin.dispose()

    engine = _engine_for(schema)
    meta = MetaData(schema=schema)
    for table in AgriBase.metadata.sorted_tables:
        table.to_metadata(meta, schema=schema)
    meta.create_all(engine)
    if has_tables:
        with engine.begin() as conn:
            for ddl in _NEW_TABLE_DDL:
                conn.execute(text(ddl))
    return engine


def _drop_schema(engine, has_tables: bool):
    engine.dispose()
    admin = create_engine(os.environ["AGRI_DB_URL"], poolclass=NullPool)
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA[has_tables]} CASCADE"))
    admin.dispose()


@pytest.fixture(scope="module")
def _schemas(django_db_setup):
    if not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"):
        pytest.skip("requires Postgres")
    engines = {shape: _build_schema(shape) for shape in (False, True)}
    yield engines
    for shape, engine in engines.items():
        _drop_schema(engine, shape)


_USER_SQL = (
    'INSERT INTO "CustomUser_customuser" '
    "(id, password, is_superuser, username, firstname, lastname, email, "
    " payement_status, is_active, is_staff, is_technician, notify_every, "
    " preferred_language, date_joined) "
    "VALUES (:id, '', false, :username, 'F', 'L', :email, 'actif', true, false, "
    " :is_technician, 240, 'fr', now())"
)

_ZONE_SQL = (
    "INSERT INTO analytics_zone "
    "(id, user_id, name, space, critical_moisture_threshold, "
    ' "soil_param_TAW", "soil_param_FC", "soil_param_WP", "soil_param_RAW", '
    " pomp_flow_rate, irrigation_water_quantity, elevation_m) "
    "VALUES (:id, :user_id, :name, 1.0, 20.0, 50.0, 50.0, 50.0, 50.0, "
    " 100.0, 100.0, 0.0)"
)

_DEVICE_SQL = (
    "INSERT INTO analytics_device "
    "(id, user_id, zone_id, device_type, serial, name, is_active, created_at) "
    "VALUES (:id, :user_id, :zone_id, 'lora', :serial, :name, true, now())"
)


def _seed(engine, has_tables: bool):
    """A farm (owner 10, zone 20, device 30) + a second farm (11/21/31) whose
    rows the owner must never reach."""
    tables = [
        "analytics_technicianzonegrant",
        "analytics_techniciangrant",
        "analytics_device",
        "analytics_zone",
        '"CustomUser_customuser"',
    ]
    if has_tables:
        tables = [
            "analytics_sensorgroupsensor",
            "analytics_sensorgroup",
            "analytics_sensorcalibration",
        ] + tables
    with Session(engine) as session:
        session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        for uid, username, is_technician in (
            (_OWNER_ID, "grp-owner", False),
            (_OTHER_ID, "grp-other", False),
            (_TECHNICIAN_ID, "grp-tech", True),
        ):
            session.execute(
                text(_USER_SQL),
                {
                    "id": uid,
                    "username": username,
                    "email": f"{username}@example.com",
                    "is_technician": is_technician,
                },
            )
        session.execute(
            text(_ZONE_SQL),
            {"id": _ZONE_ID, "user_id": _OWNER_ID, "name": "parcelle-1"},
        )
        session.execute(
            text(_ZONE_SQL),
            {"id": _OTHER_ZONE_ID, "user_id": _OTHER_ID, "name": "parcelle-2"},
        )
        session.execute(
            text(_DEVICE_SQL),
            {
                "id": _DEVICE_ID,
                "user_id": _OWNER_ID,
                "zone_id": _ZONE_ID,
                "serial": "GRP-1",
                "name": "Sensor A",
            },
        )
        session.execute(
            text(_DEVICE_SQL),
            {
                "id": _OTHER_DEVICE_ID,
                "user_id": _OTHER_ID,
                "zone_id": _OTHER_ZONE_ID,
                "serial": "GRP-2",
                "name": "Foreign sensor",
            },
        )
        session.commit()


def _authenticate_as(user_id: int, username: str, is_technician: bool = False):
    app.dependency_overrides[get_current_user] = lambda: AuthedUser(
        id=user_id,
        username=username,
        email=f"{username}@example.com",
        is_staff=False,
        is_technician=is_technician,
        preferred_language="fr",
    )


@pytest.fixture(params=[False, True], ids=["without_tables", "with_tables"])
def groups_api(request, _schemas, monkeypatch):
    """``(client, has_tables, engine)`` — a TestClient whose routers read/write
    the schema shape under test, authenticated as the farm owner (id 10)."""
    has_tables = request.param
    engine = _schemas[has_tables]
    factory = sessionmaker(bind=engine, autoflush=False, future=True)

    @contextmanager
    def _scope(*, commit: bool = False):
        session = factory()
        try:
            yield session
            if commit:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(groups_router, "session_scope", _scope)
    monkeypatch.setattr(calib_router, "session_scope", _scope)

    _seed(engine, has_tables)
    _authenticate_as(_OWNER_ID, "grp-owner")
    try:
        with TestClient(app) as client:
            yield client, has_tables, engine
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _create_group(client, name="Bloc nord", **extra):
    return client.post("/sensor-groups", json={"name": name, **extra})


def _assert_write_refused(resp):
    """The degradation contract for writes: a clear, actionable 400 that names
    the migration — never an UndefinedTable 500, never a silent success."""
    assert resp.status_code == 400, resp.text
    assert "f4b6d2e8c1a9" in resp.json()["detail"]


# --- groups ----------------------------------------------------------------
@_requires_pg
def test_create_then_list_a_group(groups_api):
    client, has_tables, _ = groups_api

    created = _create_group(client, description="Rangée 1", display_order=2)

    if not has_tables:
        _assert_write_refused(created)
        # and the read still answers, empty
        assert client.get("/sensor-groups").json() == []
        return

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Bloc nord"
    assert body["description"] == "Rangée 1"
    assert body["display_order"] == 2
    assert body["is_active"] is True
    assert body["sensors"] == []

    listed = client.get("/sensor-groups")
    assert listed.status_code == 200, listed.text
    (group,) = listed.json()
    assert group["id"] == body["id"]


@_requires_pg
def test_group_names_are_unique_per_owner(groups_api):
    client, has_tables, _ = groups_api
    first = _create_group(client)
    if not has_tables:
        _assert_write_refused(first)
        return

    assert first.status_code == 201, first.text
    clash = _create_group(client)

    assert clash.status_code == 400, clash.text
    assert "already exists" in clash.json()["detail"]


@_requires_pg
def test_update_a_group(groups_api):
    client, has_tables, _ = groups_api
    created = _create_group(client)
    if not has_tables:
        _assert_write_refused(client.patch("/sensor-groups/1", json={"name": "x"}))
        return

    pk = created.json()["id"]
    resp = client.patch(
        f"/sensor-groups/{pk}",
        json={"name": "Bloc sud", "description": "d", "is_active": False},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Bloc sud"
    assert body["description"] == "d"
    assert body["is_active"] is False


@_requires_pg
def test_delete_a_group_removes_only_its_memberships(groups_api):
    client, has_tables, engine = groups_api
    if not has_tables:
        _assert_write_refused(client.delete("/sensor-groups/1"))
        return

    pk = _create_group(client).json()["id"]
    added = client.post(
        f"/sensor-groups/{pk}/sensors",
        json={"device_id": _DEVICE_ID, "sensor_key": "soil_moisture"},
    )
    assert added.status_code == 201, added.text

    resp = client.delete(f"/sensor-groups/{pk}")

    assert resp.status_code == 200, resp.text
    assert client.get("/sensor-groups").json() == []
    with Session(engine) as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM analytics_sensorgroupsensor")
            ).scalar_one()
            == 0
        )
        # the device itself survives — a group is a view, not ownership
        assert (
            session.execute(
                text("SELECT count(*) FROM analytics_device WHERE id = :d"),
                {"d": _DEVICE_ID},
            ).scalar_one()
            == 1
        )


@_requires_pg
def test_add_and_remove_a_sensor(groups_api):
    client, has_tables, _ = groups_api
    if not has_tables:
        _assert_write_refused(
            client.post(
                "/sensor-groups/1/sensors",
                json={"device_id": _DEVICE_ID, "sensor_key": "soil_moisture"},
            )
        )
        _assert_write_refused(client.delete("/sensor-groups/1/sensors/1"))
        return

    pk = _create_group(client).json()["id"]
    added = client.post(
        f"/sensor-groups/{pk}/sensors",
        json={"device_id": _DEVICE_ID, "sensor_key": "soil_moisture", "label": "S1"},
    )

    assert added.status_code == 201, added.text
    member = added.json()
    assert member["sensor_key"] == "soil_moisture"
    assert member["label"] == "S1"
    # the zone defaults to the device's own zone
    assert member["zone_id"] == _ZONE_ID
    assert member["device_serial"] == "GRP-1"

    (group,) = client.get("/sensor-groups").json()
    assert [s["id"] for s in group["sensors"]] == [member["id"]]

    # the same sensor twice in one group is refused (UNIQUE group/device/key)
    again = client.post(
        f"/sensor-groups/{pk}/sensors",
        json={"device_id": _DEVICE_ID, "sensor_key": "soil_moisture"},
    )
    assert again.status_code == 400, again.text

    removed = client.delete(f"/sensor-groups/{pk}/sensors/{member['id']}")
    assert removed.status_code == 200, removed.text
    (group,) = client.get("/sensor-groups").json()
    assert group["sensors"] == []


@_requires_pg
def test_one_sensor_can_belong_to_two_groups(groups_api):
    client, has_tables, _ = groups_api
    if not has_tables:
        pytest.skip("membership needs the tables; the refusal is covered above")

    first = _create_group(client, name="Bloc nord").json()["id"]
    second = _create_group(client, name="Rangée A").json()["id"]
    sensor = {"device_id": _DEVICE_ID, "sensor_key": "soil_temperature"}

    a = client.post(f"/sensor-groups/{first}/sensors", json=sensor)
    b = client.post(f"/sensor-groups/{second}/sensors", json=sensor)

    assert (a.status_code, b.status_code) == (201, 201), (a.text, b.text)
    groups = {g["id"]: g for g in client.get("/sensor-groups").json()}
    assert [s["sensor_key"] for s in groups[first]["sensors"]] == ["soil_temperature"]
    assert [s["sensor_key"] for s in groups[second]["sensors"]] == ["soil_temperature"]


@_requires_pg
def test_a_foreign_device_cannot_be_added_to_a_group(groups_api):
    client, has_tables, _ = groups_api
    if not has_tables:
        pytest.skip("membership needs the tables; the refusal is covered above")

    pk = _create_group(client).json()["id"]

    resp = client.post(
        f"/sensor-groups/{pk}/sensors",
        json={"device_id": _OTHER_DEVICE_ID, "sensor_key": "soil_moisture"},
    )

    assert resp.status_code == 404, resp.text


@_requires_pg
def test_cross_owner_group_access_is_denied(groups_api):
    client, has_tables, _ = groups_api
    if not has_tables:
        pytest.skip("cross-owner scoping needs the tables; refusal covered above")

    pk = _create_group(client).json()["id"]

    _authenticate_as(_OTHER_ID, "grp-other")
    assert client.get("/sensor-groups").json() == []
    assert (
        client.patch(f"/sensor-groups/{pk}", json={"name": "mine"}).status_code == 404
    )
    assert client.delete(f"/sensor-groups/{pk}").status_code == 404
    assert (
        client.post(
            f"/sensor-groups/{pk}/sensors",
            json={"device_id": _OTHER_DEVICE_ID, "sensor_key": "soil_moisture"},
        ).status_code
        == 404
    )
    # ... and the owner still has their group
    _authenticate_as(_OWNER_ID, "grp-owner")
    assert [g["id"] for g in client.get("/sensor-groups").json()] == [pk]


@_requires_pg
def test_a_technician_reads_the_owners_groups_but_cannot_write(groups_api):
    client, has_tables, engine = groups_api
    if not has_tables:
        pytest.skip("technician scoping needs the tables; refusal covered above")

    pk = _create_group(client).json()["id"]
    client.post(
        f"/sensor-groups/{pk}/sensors",
        json={"device_id": _DEVICE_ID, "sensor_key": "soil_moisture"},
    )
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO analytics_techniciangrant "
                "(id, technician_id, owner_id, is_active, created_at) "
                "VALUES (1, :tid, :oid, true, now())"
            ),
            {"tid": _TECHNICIAN_ID, "oid": _OWNER_ID},
        )
        session.execute(
            text(
                "INSERT INTO analytics_technicianzonegrant "
                "(id, grant_id, zone_id, allowed_graphs) "
                "VALUES (1, 1, :zone, '[]'::jsonb)"
            ),
            {"zone": _ZONE_ID},
        )
        session.commit()

    _authenticate_as(_TECHNICIAN_ID, "grp-tech", is_technician=True)
    listed = client.get("/sensor-groups")
    assert listed.status_code == 200, listed.text
    (group,) = listed.json()
    assert group["id"] == pk
    assert len(group["sensors"]) == 1

    assert _create_group(client, name="tech group").status_code == 403
    assert client.delete(f"/sensor-groups/{pk}").status_code == 403


# --- calibration -----------------------------------------------------------
@_requires_pg
def test_calibration_defaults_to_the_identity_transform(groups_api):
    client, has_tables, _ = groups_api

    resp = client.get(f"/sensor-calibrations/{_DEVICE_ID}/soil_moisture")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["scale_a"], body["offset_b"]) == (1.0, 0.0)
    assert body["configured"] is False
    assert body["schema_available"] is has_tables


@_requires_pg
def test_upsert_calibration_twice_is_idempotent(groups_api):
    client, has_tables, engine = groups_api
    payload = {"scale_a": 1.05, "offset_b": -0.4, "unit": "%", "note": "terrain"}

    first = client.put(f"/sensor-calibrations/{_DEVICE_ID}/soil_moisture", json=payload)

    if not has_tables:
        _assert_write_refused(first)
        return

    assert first.status_code == 201, first.text
    assert first.json()["scale_a"] == 1.05
    assert first.json()["configured"] is True

    second = client.put(
        f"/sensor-calibrations/{_DEVICE_ID}/soil_moisture",
        json={**payload, "offset_b": 0.2},
    )
    assert second.status_code == 200, second.text
    assert second.json()["offset_b"] == 0.2

    with Session(engine) as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM analytics_sensorcalibration")
            ).scalar_one()
            == 1
        )
    read_back = client.get(f"/sensor-calibrations/{_DEVICE_ID}/soil_moisture").json()
    assert read_back["offset_b"] == 0.2
    assert read_back["unit"] == "%"
    assert read_back["note"] == "terrain"


@_requires_pg
def test_calibration_is_per_device_and_sensor_key(groups_api):
    client, has_tables, _ = groups_api
    if not has_tables:
        pytest.skip("needs the calibration table; the refusal is covered above")

    client.put(
        f"/sensor-calibrations/{_DEVICE_ID}/soil_moisture", json={"scale_a": 2.0}
    )

    other_key = client.get(f"/sensor-calibrations/{_DEVICE_ID}/soil_temperature").json()
    assert other_key["configured"] is False
    assert other_key["scale_a"] == 1.0


@_requires_pg
def test_calibrating_a_foreign_device_is_denied(groups_api):
    client, _has_tables, _ = groups_api

    read = client.get(f"/sensor-calibrations/{_OTHER_DEVICE_ID}/soil_moisture")
    write = client.put(
        f"/sensor-calibrations/{_OTHER_DEVICE_ID}/soil_moisture", json={"scale_a": 2.0}
    )

    # ownership is checked against analytics_device, which exists in BOTH
    # shapes — the shim must never widen access
    assert read.status_code == 404, read.text
    assert write.status_code == 404, write.text


@_requires_pg
def test_calibration_rejects_a_zero_scale(groups_api):
    client, _has_tables, _ = groups_api

    resp = client.put(
        f"/sensor-calibrations/{_DEVICE_ID}/soil_moisture", json={"scale_a": 0}
    )

    assert resp.status_code == 400, resp.text
    assert "scale_a" in resp.json()["detail"]
