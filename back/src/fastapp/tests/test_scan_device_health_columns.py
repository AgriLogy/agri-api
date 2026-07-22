"""Regression (#432): ``scan_device_health`` must not SELECT the whole
``analytics_device`` entity.

The full-entity select emitted ``latitude``/``longitude`` — columns that only
exist once the agri-db device-map migration has been applied — so the beat job
died with ``UndefinedColumn`` on every run against a schema without them. The
task reads only id/serial/name/user_id/last_health_notified, so its query must
name exactly those and stay valid on BOTH schema states.

Pure unit test: the session is a stub that records the statements, so this runs
everywhere (no Postgres, no Django DB).
"""

from __future__ import annotations

import contextlib

from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from fastapp import tasks_scan


class _Result:
    def all(self):
        return []

    def first(self):
        return None

    rowcount = 0


class _RecordingSession:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        return _Result()


def _run_and_capture(monkeypatch):
    session = _RecordingSession()

    @contextlib.contextmanager
    def _fake_scope(*args, **kwargs):
        yield session

    monkeypatch.setattr(tasks_scan, "session_scope", _fake_scope)
    result = tasks_scan.scan_device_health()
    return session, result


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


def test_device_scan_query_omits_latitude_longitude(monkeypatch):
    session, result = _run_and_capture(monkeypatch)

    assert result == {"scanned": 0, "notified": 0, "healthy": 0, "skipped": 0}
    assert session.statements, "the scan must issue at least one query"

    device_selects = [
        _sql(s) for s in session.statements if "analytics_device" in _sql(s)
    ]
    assert device_selects, "the scan must query analytics_device"
    for sql in device_selects:
        assert "latitude" not in sql
        assert "longitude" not in sql


def test_device_scan_query_selects_only_needed_columns(monkeypatch):
    session, _ = _run_and_capture(monkeypatch)

    sql = _sql(session.statements[0])
    assert sql.startswith("select")
    for column in (
        "analytics_device.id",
        "analytics_device.serial",
        "analytics_device.name",
        "analytics_device.user_id",
        "analytics_device.last_health_notified",
    ):
        assert column in sql
    # entity-wide columns the task never reads must not be pulled in
    for column in ("analytics_device.device_type", "analytics_device.created_at"):
        assert column not in sql


# --- the same thing proven end-to-end against a schema WITHOUT the columns ---
# A hand-rolled sqlite mirror of the production ``analytics_device`` shape as it
# exists today: no latitude/longitude (the device-map migration is not applied).
# The task must run to completion on it. ``user_id`` is left NULL so the scan
# stops at "no recipient" and never needs the users table.
_DEVICE_DDL_NO_COORDS = """
CREATE TABLE analytics_device (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    zone_id INTEGER,
    device_type VARCHAR(20) NOT NULL,
    serial VARCHAR(128) NOT NULL,
    name VARCHAR(120) NOT NULL,
    is_active BOOLEAN NOT NULL,
    last_health_notified TIMESTAMP,
    created_at TIMESTAMP
)
"""

_UPLINK_DDL = """
CREATE TABLE lora_uplink (
    id INTEGER PRIMARY KEY,
    dev_eui VARCHAR(64),
    received_at TIMESTAMP,
    battery_v FLOAT
)
"""


def test_scan_runs_against_schema_without_coordinate_columns(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(_DEVICE_DDL_NO_COORDS))
        conn.execute(text(_UPLINK_DDL))
        conn.execute(
            text(
                "INSERT INTO analytics_device "
                "(id, user_id, zone_id, device_type, serial, name, is_active, "
                " last_health_notified, created_at) "
                "VALUES (1, NULL, NULL, 'lora', 'AA01', 'Dev', 1, NULL, NULL)"
            )
        )

    session = Session(engine)

    @contextlib.contextmanager
    def _fake_scope(*args, **kwargs):
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(tasks_scan, "session_scope", _fake_scope)
    monkeypatch.setattr(tasks_scan, "_send_email", lambda **kwargs: True)

    # No uplink → offline → owner lookup → no recipient → skipped. What matters
    # is that the device SELECT itself executes on a coordinate-less schema.
    assert tasks_scan.scan_device_health() == {
        "scanned": 1,
        "notified": 0,
        "healthy": 0,
        "skipped": 1,
    }
