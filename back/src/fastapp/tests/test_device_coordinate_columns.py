"""Regression (#436): the device-map endpoints must survive a schema WITHOUT
``analytics_device.latitude`` / ``longitude``.

Those columns only exist once the agri-db migration ``b2c3d4e5f6a7`` is
applied — and it is deliberately held, so production has neither. Naming them
unconditionally made ``GET /my-devices`` (the farmer map) and the admin
``/devices`` CRUD die with ``psycopg.errors.UndefinedColumn``.

The shim under test is ``fastapp.schema_compat``: one cached, dialect-neutral
column probe per engine, consulted by the two routers that genuinely need the
coordinates.

Two levels:

* unit — the probe answers correctly for both schema shapes and is evaluated
  ONCE per engine, not per call (in-memory sqlite / a stub session: no
  Postgres, no Django DB);
* integration — the REAL endpoints over ``TestClient``, against a real
  Postgres schema built in both shapes (production's, without the columns, and
  the post-migration one), parametrized.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import agri.db.analytics  # noqa: F401  (registers every table on AgriBase)
import agri.db.devices  # noqa: F401
import agri.db.irrigation  # noqa: F401
import agri.db.users  # noqa: F401
from agri.db.base import AgriBase
from fastapp import schema_compat
from fastapp.auth import AuthedUser, get_current_user
from fastapp.main import app

# ---------------------------------------------------------------------------
# The two shapes of ``analytics_device``: production today (the migration is
# held) and post-migration. Hand-rolled so the "without" shape is a genuine
# table missing genuine columns, not a mocked one.
# ---------------------------------------------------------------------------
_DEVICE_COLUMNS = """
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    zone_id BIGINT,
    device_type VARCHAR(20) NOT NULL,
    serial VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(120) NOT NULL,
    is_active BOOLEAN NOT NULL,
    last_health_notified TIMESTAMPTZ,
    created_at TIMESTAMPTZ
"""
_COORDINATE_COLUMNS = (
    ",\n    latitude DOUBLE PRECISION,\n    longitude DOUBLE PRECISION"
)

_SQLITE_DEVICE_DDL = {
    False: f"CREATE TABLE analytics_device ({_DEVICE_COLUMNS.replace('BIGSERIAL', 'INTEGER')})",
    True: (
        "CREATE TABLE analytics_device ("
        f"{_DEVICE_COLUMNS.replace('BIGSERIAL', 'INTEGER')}{_COORDINATE_COLUMNS})"
    ),
}


# ---------------------------------------------------------------------------
# Unit level — no Postgres, no Django DB.
# ---------------------------------------------------------------------------
class _StubBind:
    """A weakref-able stand-in for an Engine (the probe cache keys on it)."""


class _StubSession:
    def __init__(self, bind):
        self._bind = bind

    def get_bind(self):
        return self._bind


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    schema_compat.reset_device_coordinates_cache()
    yield
    schema_compat.reset_device_coordinates_cache()


def _sqlite_engine(has_coordinates: bool):
    # default pool (a single shared connection): an in-memory sqlite DB lives
    # and dies with its connection, so NullPool would throw the table away.
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(_SQLITE_DEVICE_DDL[has_coordinates]))
    return engine


@pytest.mark.parametrize("has_coordinates", [False, True], ids=["absent", "present"])
def test_probe_reports_the_real_schema_shape(has_coordinates):
    engine = _sqlite_engine(has_coordinates)
    session = Session(engine)
    try:
        assert schema_compat.device_coordinates_available(session) is has_coordinates
    finally:
        session.close()
        engine.dispose()


def test_probe_reports_absent_when_the_table_itself_is_missing():
    # A brand-new database with no analytics_device at all must degrade, not
    # raise — the shim's job is to never turn a schema gap into a 500.
    engine = create_engine("sqlite://")
    session = Session(engine)
    try:
        assert schema_compat.device_coordinates_available(session) is False
    finally:
        session.close()
        engine.dispose()


def test_probe_runs_once_per_engine_not_once_per_call(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        schema_compat, "_probe", lambda engine: (calls.append(engine), True)[1]
    )
    bind = _StubBind()
    session = _StubSession(bind)

    results = [schema_compat.device_coordinates_available(session) for _ in range(25)]

    assert results == [True] * 25
    assert len(calls) == 1, "the column probe must be cached, not re-run per request"


def test_probe_is_cached_per_engine(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        schema_compat, "_probe", lambda engine: (calls.append(engine), True)[1]
    )
    first, second = _StubBind(), _StubBind()

    schema_compat.device_coordinates_available(_StubSession(first))
    schema_compat.device_coordinates_available(_StubSession(second))
    schema_compat.device_coordinates_available(_StubSession(first))

    # one probe per distinct engine, and never a second one for the same engine
    assert calls == [first, second]


def test_reset_clears_the_cache(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        schema_compat, "_probe", lambda engine: (calls.append(engine), True)[1]
    )
    session = _StubSession(_StubBind())

    schema_compat.device_coordinates_available(session)
    schema_compat.reset_device_coordinates_cache()
    schema_compat.device_coordinates_available(session)

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Integration level — the real endpoints against a real Postgres schema, built
# once in each shape. The tables live in their own schema (search_path) so the
# Django test database is untouched: the routers' unqualified SQL resolves
# there, exactly as it resolves to ``public`` in production.
# ---------------------------------------------------------------------------
_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="the device-map endpoints are exercised against a real Postgres "
    "schema (fastapp reads the test DB over SQLAlchemy)",
)

_SCHEMA = {False: "compat_no_device_coords", True: "compat_device_coords"}


def _engine_for(schema: str):
    return create_engine(
        os.environ["AGRI_DB_URL"],
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={schema}"},
    )


def _build_schema(has_coordinates: bool):
    """Create ``<schema>`` holding every agri-db table plus an
    ``analytics_device`` in the requested shape. Returns the bound engine."""
    schema = _SCHEMA[has_coordinates]
    admin = create_engine(os.environ["AGRI_DB_URL"], poolclass=NullPool)
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    admin.dispose()

    engine = _engine_for(schema)
    meta = MetaData(schema=schema)
    for table in AgriBase.metadata.sorted_tables:
        if table.name != "analytics_device":
            table.to_metadata(meta, schema=schema)
    meta.create_all(engine)
    device_ddl = (
        f"CREATE TABLE {schema}.analytics_device ({_DEVICE_COLUMNS}"
        f"{_COORDINATE_COLUMNS if has_coordinates else ''})"
    )
    with engine.begin() as conn:
        conn.execute(text(device_ddl))
    return engine


def _drop_schema(engine, has_coordinates: bool):
    engine.dispose()
    admin = create_engine(os.environ["AGRI_DB_URL"], poolclass=NullPool)
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA[has_coordinates]} CASCADE"))
    admin.dispose()


@pytest.fixture(scope="module")
def _schemas(django_db_setup):
    if not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"):
        pytest.skip("requires Postgres")
    engines = {shape: _build_schema(shape) for shape in (False, True)}
    yield engines
    for shape, engine in engines.items():
        _drop_schema(engine, shape)


@pytest.fixture(params=[False, True], ids=["without_coordinates", "with_coordinates"])
def device_api(request, _schemas, monkeypatch):
    """``(client, has_coordinates)`` — a TestClient whose routers read/write the
    schema shape under test, authenticated as a staff user (id 10).

    The routers' ``session_scope`` is redirected at that schema; authentication
    is replaced by a dependency override so the test needs no token minting.
    """
    has_coordinates = request.param
    engine = _schemas[has_coordinates]
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

    from fastapp.routers import devices as devices_router
    from fastapp.routers import selfreads as selfreads_router

    monkeypatch.setattr(devices_router, "session_scope", _scope)
    monkeypatch.setattr(selfreads_router, "session_scope", _scope)

    with factory() as session:
        session.execute(
            text(
                'TRUNCATE analytics_device, analytics_zone, "CustomUser_customuser" '
                "RESTART IDENTITY CASCADE"
            )
        )
        session.execute(
            text(
                'INSERT INTO "CustomUser_customuser" '
                "(id, password, is_superuser, username, firstname, lastname, email, "
                " payement_status, is_active, is_staff, is_technician, notify_every, "
                " preferred_language, date_joined) "
                "VALUES (10, '', false, 'map-owner', 'Map', 'Owner', "
                " 'map-owner@example.com', 'actif', true, true, false, 240, 'fr', now())"
            )
        )
        session.execute(
            text(
                "INSERT INTO analytics_zone "
                "(id, user_id, name, space, critical_moisture_threshold, "
                ' "soil_param_TAW", "soil_param_FC", "soil_param_WP", "soil_param_RAW", '
                " pomp_flow_rate, irrigation_water_quantity, elevation_m) "
                "VALUES (20, 10, 'parcelle-1', 1.0, 20.0, 50.0, 50.0, 50.0, 50.0, "
                " 100.0, 100.0, 0.0)"
            )
        )
        session.commit()

    app.dependency_overrides[get_current_user] = lambda: AuthedUser(
        id=10,
        username="map-owner",
        email="map-owner@example.com",
        is_staff=True,
        is_technician=False,
        preferred_language="fr",
    )
    try:
        with TestClient(app) as client:
            yield client, has_coordinates
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _seed_device(engine, *, has_coordinates: bool, serial: str = "MAP-1"):
    columns = "id, user_id, zone_id, device_type, serial, name, is_active, created_at"
    values = f"30, 10, 20, 'lora', '{serial}', 'Sensor A', true, now()"
    if has_coordinates:
        columns += ", latitude, longitude"
        values += ", 33.5731, -7.5898"
    with Session(engine) as session:
        session.execute(
            text(f"INSERT INTO analytics_device ({columns}) VALUES ({values})")
        )
        session.commit()


@_requires_pg
def test_my_devices_returns_200_with_null_coordinates_when_columns_are_absent(
    device_api, _schemas
):
    client, has_coordinates = device_api
    _seed_device(_schemas[has_coordinates], has_coordinates=has_coordinates)

    resp = client.get("/my-devices")

    assert resp.status_code == 200, resp.text
    (device,) = resp.json()
    assert set(device) == {
        "id",
        "device_type",
        "serial",
        "name",
        "zone",
        "latitude",
        "longitude",
    }
    assert device["serial"] == "MAP-1"
    if has_coordinates:
        assert (device["latitude"], device["longitude"]) == (33.5731, -7.5898)
    else:
        # the honest state today: no pin on the map, but a working map
        assert device["latitude"] is None
        assert device["longitude"] is None


@_requires_pg
def test_admin_device_list_returns_200_either_way(device_api, _schemas):
    client, has_coordinates = device_api
    _seed_device(_schemas[has_coordinates], has_coordinates=has_coordinates)

    resp = client.get("/devices")

    assert resp.status_code == 200, resp.text
    (device,) = resp.json()
    assert device["serial"] == "MAP-1"
    assert device["user"] == "map-owner"
    if has_coordinates:
        assert (device["latitude"], device["longitude"]) == (33.5731, -7.5898)
    else:
        assert device["latitude"] is None
        assert device["longitude"] is None


@_requires_pg
def test_create_device_without_coordinates_succeeds_either_way(device_api):
    client, has_coordinates = device_api

    resp = client.post(
        "/devices",
        json={
            "device_type": "lora",
            "serial": "MAP-NEW",
            "name": "Fresh",
            "username": "map-owner",
            "zone_id": 20,
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["serial"] == "MAP-NEW"
    assert body["latitude"] is None and body["longitude"] is None


@_requires_pg
def test_create_device_with_coordinates_is_refused_until_the_migration_lands(
    device_api,
):
    client, has_coordinates = device_api

    resp = client.post(
        "/devices",
        json={
            "device_type": "lora",
            "serial": "MAP-GPS",
            "name": "Pinned",
            "username": "map-owner",
            "zone_id": 20,
            "latitude": 34.02,
            "longitude": -6.83,
        },
    )

    if has_coordinates:
        assert resp.status_code == 201, resp.text
        assert resp.json()["latitude"] == 34.02
        assert resp.json()["longitude"] == -6.83
    else:
        # a clear, actionable 400 — never a raw UndefinedColumn, and never a
        # silent success that drops the position the admin just entered
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert "latitude/longitude" in detail
        assert "b2c3d4e5f6a7" in detail


@_requires_pg
def test_patch_device_with_coordinates_is_refused_until_the_migration_lands(
    device_api, _schemas
):
    client, has_coordinates = device_api
    _seed_device(_schemas[has_coordinates], has_coordinates=has_coordinates)

    resp = client.patch("/devices/30", json={"latitude": 34.02, "longitude": -6.83})

    if has_coordinates:
        assert resp.status_code == 200, resp.text
        assert resp.json()["latitude"] == 34.02
    else:
        assert resp.status_code == 400, resp.text
        assert "b2c3d4e5f6a7" in resp.json()["detail"]


@_requires_pg
def test_patch_device_without_coordinates_still_updates_either_way(
    device_api, _schemas
):
    client, has_coordinates = device_api
    _seed_device(_schemas[has_coordinates], has_coordinates=has_coordinates)

    resp = client.patch("/devices/30", json={"name": "Renamed"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"
