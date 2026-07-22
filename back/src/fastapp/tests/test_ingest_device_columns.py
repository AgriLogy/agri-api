"""Regression (#434): ``resolve_device`` must not SELECT the whole
``analytics_device`` entity.

The full-entity select emitted ``latitude``/``longitude`` — columns that only
exist once the agri-db device-map migration has been applied — so the LIVE
LoRa/MQTT uplink path would die with ``UndefinedColumn`` on every frame against
a schema without them. Resolution reads only id/is_active/zone_id/user_id, so
its query must name exactly those and stay valid on BOTH schema states.

Two levels, both database-cheap (in-memory sqlite / a stub session), so the
whole file runs everywhere — no Postgres, no Django DB, no broker:

* unit — the compiled SQL of the resolution query, plus ``resolve_device``'s
  return contract for the ordinary and not-found cases;
* integration — a realistic ChirpStack uplink driven through
  ``handle_chirpstack_uplink`` against a sqlite mirror of the production schema
  BOTH without and with the coordinate columns.
"""

from __future__ import annotations

import types

import pytest
from sqlalchemy import JSON, Integer, MetaData, create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

import agri.db.analytics  # noqa: F401  (registers every table on AgriBase)
import agri.db.devices  # noqa: F401
import agri.db.irrigation  # noqa: F401
import agri.db.users  # noqa: F401
from agri.db.base import AgriBase
from fastapp import ingest

# ---------------------------------------------------------------------------
# Unit level — no database at all: a stub session records the statements.
# ---------------------------------------------------------------------------
_NEEDED_COLUMNS = (
    "analytics_device.id",
    "analytics_device.is_active",
    "analytics_device.zone_id",
    "analytics_device.user_id",
)


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _RecordingSession:
    """Minimal stand-in for a SQLAlchemy Session: records every statement and
    always answers with the same (possibly ``None``) device row."""

    def __init__(self, row=None):
        self.statements = []
        self._row = row

    def execute(self, statement, params=None):
        self.statements.append(statement)
        return _Result(self._row)


def _device_row(*, device_id=7, is_active=True, zone_id=3, user_id=5):
    """A ``Row``-alike: ``resolve_device`` only ever reads it by attribute."""
    return types.SimpleNamespace(
        id=device_id, is_active=is_active, zone_id=zone_id, user_id=user_id
    )


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


def test_device_resolution_query_omits_latitude_longitude():
    session = _RecordingSession(_device_row())

    ingest.resolve_device(session, "AA11BB22CC33DD44")

    device_selects = [
        _sql(s) for s in session.statements if "analytics_device" in _sql(s)
    ]
    assert device_selects, "resolution must query analytics_device"
    for sql in device_selects:
        assert "latitude" not in sql
        assert "longitude" not in sql


def test_device_resolution_query_selects_only_needed_columns():
    session = _RecordingSession(_device_row())

    ingest.resolve_device(session, "AA11BB22CC33DD44")

    sql = _sql(session.statements[0])
    assert sql.startswith("select")
    for column in _NEEDED_COLUMNS:
        assert column in sql
    # entity-wide columns resolution never reads must not be pulled in
    for column in (
        "analytics_device.serial,",
        "analytics_device.name",
        "analytics_device.device_type",
        "analytics_device.last_health_notified",
        "analytics_device.created_at",
    ):
        assert column not in sql


def test_resolve_device_returns_owner_for_active_assigned_device():
    session = _RecordingSession(_device_row())

    assert ingest.resolve_device(session, "AA11BB22CC33DD44") == (7, 5, 3)


@pytest.mark.parametrize(
    "row",
    [
        _device_row(is_active=False),  # deactivated device
        _device_row(zone_id=None),  # registered but not assigned to a zone
    ],
    ids=["inactive", "unassigned"],
)
def test_resolve_device_withholds_owner_when_not_active_and_assigned(row):
    # device_id is still returned so the reading can be stamped; ownership
    # falls through to the shared ``lora`` catch-all.
    session = _RecordingSession(row)

    assert ingest.resolve_device(session, "AA11BB22CC33DD44") == (7, None, None)


def test_resolve_device_not_found_returns_all_none(monkeypatch):
    # the auto-register INSERT raced away — nothing to resolve, no crash.
    monkeypatch.setattr(
        ingest, "auto_register_lora_device", lambda *a, **k: None, raising=True
    )
    session = _RecordingSession(None)

    assert ingest.resolve_device(session, "AA11BB22CC33DD44") == (None, None, None)
    # both attempts (before and after auto-registration) hit the same query,
    # so the column scoping above covers the retry too.
    assert len(session.statements) == 2


# ---------------------------------------------------------------------------
# Integration level — a realistic uplink driven end to end through the ingest
# entry point against a sqlite mirror of the production schema.
#
# ``analytics_device`` is hand-rolled in BOTH shapes: today's production table
# (no latitude/longitude — the device-map migration is not applied) and the
# post-migration one. Everything else is generated from the agri-db metadata,
# retyped for sqlite (BigInteger identity PKs → INTEGER rowid so they
# autoincrement; JSONB → JSON, which sqlite can render).
# ---------------------------------------------------------------------------
_DEVICE_DDL_NO_COORDS = """
CREATE TABLE analytics_device (
    id INTEGER PRIMARY KEY,
    user_id BIGINT NOT NULL,
    zone_id BIGINT,
    device_type VARCHAR(20) NOT NULL,
    serial VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(120) NOT NULL,
    is_active BOOLEAN NOT NULL,
    last_health_notified TIMESTAMP,
    created_at TIMESTAMP
)
"""

_DEVICE_DDL_WITH_COORDS = """
CREATE TABLE analytics_device (
    id INTEGER PRIMARY KEY,
    user_id BIGINT NOT NULL,
    zone_id BIGINT,
    device_type VARCHAR(20) NOT NULL,
    serial VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(120) NOT NULL,
    is_active BOOLEAN NOT NULL,
    last_health_notified TIMESTAMP,
    created_at TIMESTAMP,
    latitude FLOAT,
    longitude FLOAT
)
"""


def _sqlite_engine(device_ddl: str):
    meta = MetaData()
    sources = [
        t for t in AgriBase.metadata.sorted_tables if t.name != "analytics_device"
    ]
    sources += list(ingest._IngestBase.metadata.sorted_tables)
    for table in sources:
        copy = table.to_metadata(meta)
        for column in copy.columns:
            column.identity = None
            if column.primary_key:
                column.type = Integer()
                column.autoincrement = True
            elif isinstance(column.type, JSONB):
                column.type = JSON()

    engine = create_engine("sqlite://")
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(device_ddl))
    return engine


def _seed_owner(session: Session, *, dev_eui: str) -> tuple[int, int]:
    """A farmer with one zone and one active device assigned to it — the
    ordinary production shape. Returns ``(user_id, zone_id)``."""
    session.execute(
        text(
            'INSERT INTO "CustomUser_customuser" '
            "(id, password, is_superuser, username, firstname, lastname, email, "
            " payement_status, is_active, is_staff, is_technician, notify_every, "
            " preferred_language, date_joined) "
            "VALUES (10, '', 0, 'farmer', 'F', 'B', 'farmer@example.com', "
            " 'actif', 1, 0, 0, 240, 'fr', '2026-01-01 00:00:00')"
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
    session.execute(
        text(
            "INSERT INTO analytics_device "
            "(id, user_id, zone_id, device_type, serial, name, is_active, "
            " last_health_notified, created_at) "
            "VALUES (30, 10, 20, 'lora', :serial, 'ph-node', 1, NULL, NULL)"
        ),
        {"serial": dev_eui},
    )
    session.flush()
    return 10, 20


def _uplink(session: Session, dev_eui: str) -> int:
    """A realistic ChirpStack v4 RS485-LB pH frame (pH + battery + RSSI)."""
    return ingest.handle_chirpstack_uplink(
        session,
        dev_eui=dev_eui,
        device_name="ph-node",
        f_cnt=42,
        f_port=2,
        rssi=-70.0,
        snr=9.0,
        frequency=868100000,
        obj={"pH": 6.4, "BatV": 3.6},
        data="",
    )


@pytest.fixture(params=["without_coordinates", "with_coordinates"])
def ingest_session(request):
    """A live sqlite session on the pre-migration schema (production today) and
    on the post-migration one — the fix must hold on both."""
    ddl = (
        _DEVICE_DDL_NO_COORDS
        if request.param == "without_coordinates"
        else _DEVICE_DDL_WITH_COORDS
    )
    session = Session(_sqlite_engine(ddl))
    try:
        yield session
    finally:
        session.close()


def test_uplink_writes_reading_attributed_to_the_device_owner(ingest_session):
    dev_eui = "AA11BB22CC33DD44"
    user_id, zone_id = _seed_owner(ingest_session, dev_eui=dev_eui)

    # pH + battery + signal
    assert _uplink(ingest_session, dev_eui) == 3
    ingest_session.commit()

    rows = ingest_session.execute(
        text("SELECT user_id, zone_id, value, device_id FROM analytics_phsoil")
    ).all()
    assert rows == [(user_id, zone_id, 6.4, 30)]

    # the raw frame is archived too, and no second device row was registered
    assert (
        ingest_session.execute(
            text("SELECT COUNT(*) FROM lora_uplink WHERE dev_eui = :s"), {"s": dev_eui}
        ).scalar()
        == 1
    )
    assert (
        ingest_session.execute(text("SELECT COUNT(*) FROM analytics_device")).scalar()
        == 1
    )


def test_uplink_from_unknown_deveui_auto_registers_and_still_writes(ingest_session):
    dev_eui = "FFEEDDCCBBAA9988"

    assert _uplink(ingest_session, dev_eui) == 3
    ingest_session.commit()

    device_id, owner_id, device_zone = ingest_session.execute(
        text("SELECT id, user_id, zone_id FROM analytics_device WHERE serial = :s"),
        {"s": dev_eui},
    ).one()
    # auto-registered as unassigned under the ``lora`` placeholder owner
    assert device_zone is None

    lora_zone_id, lora_user_id = ingest_session.execute(
        text("SELECT id, user_id FROM analytics_zone WHERE name = 'lora'")
    ).one()
    assert owner_id == lora_user_id

    # the reading lands in the shared ``lora`` catch-all, stamped with the
    # device so it follows on assignment
    rows = ingest_session.execute(
        text("SELECT user_id, zone_id, value, device_id FROM analytics_phsoil")
    ).all()
    assert rows == [(lora_user_id, lora_zone_id, 6.4, device_id)]
