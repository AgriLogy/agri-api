"""Alert-event + irrigation-decision history and the /reports endpoints (#441).

RPT-1 records two things the platform used to throw away — an alert firing and
a computed irrigation recommendation — into ``analytics_alertevent`` /
``analytics_irrigationdecision`` (agri-db migration ``e7a1c3d5b209``, which
production has NOT applied).

Two properties are load-bearing and are tested as such:

1. **The rule is SNAPSHOTTED at firing time.** Editing (or deleting) the alert
   afterwards must not rewrite history.
2. **Recording never breaks what it records.** Every write goes through
   ``fastapp.history.best_effort``; ingest must still persist the reading and
   still enqueue the alert email when the history table is missing, and when
   the history INSERT itself blows up.

   REMOVE THE WRAPPER AND: with the tables absent every alert firing raises
   ``UndefinedTable`` inside ``dispatch_alerts_for_reading`` — which
   ``handle_metrics`` catches, so the alert is silently NOT SENT — and every
   assistant/notification recommendation raises out of the handler. With the
   tables present but a single bad INSERT (e.g. a value too wide for its
   column), Postgres aborts the surrounding transaction, so the SENSOR READING
   ITSELF is rolled back: an observability feature would start eating data.
   ``test_ingest_survives_*`` / ``test_best_effort_*`` below are exactly those
   cases.

Two levels:

* unit — the row builders (snapshotting, field mapping) and the best-effort
  wrapper, with stub sessions / no database at all;
* integration — the REAL write paths and the REAL endpoints over
  ``TestClient`` against real Postgres schemas built in BOTH shapes (with the
  history tables, and without), parametrized.
"""

from __future__ import annotations

import datetime
import logging
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
from fastapp import celery as fastapp_celery
from fastapp import history, schema_compat
from fastapp.assistant import tools as assistant_tools
from fastapp.auth import AuthedUser, get_current_user
from fastapp.main import app
from fastapp.routers import ingest as ingest_router
from fastapp.routers import reports as reports_router
from fastapp.routers.selfreads import _ReadScope

# The two tables the held migration creates, verbatim from agri-db#68
# (revision e7a1c3d5b209) minus the FKs the test schema does not need.
_ALERT_EVENT_DDL = """
CREATE TABLE analytics_alertevent (
    id BIGSERIAL PRIMARY KEY,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    alert_id BIGINT REFERENCES analytics_alert (id) ON DELETE SET NULL,
    user_id BIGINT NOT NULL,
    zone_id BIGINT REFERENCES analytics_zone (id) ON DELETE SET NULL,
    notification_zone_id BIGINT,
    device_id BIGINT,
    sensor_key VARCHAR(64) NOT NULL,
    alert_name VARCHAR(200) NOT NULL DEFAULT '',
    condition VARCHAR(1) NOT NULL,
    threshold_value DOUBLE PRECISION NOT NULL,
    observed_value DOUBLE PRECISION NOT NULL,
    reading_at TIMESTAMPTZ,
    unit VARCHAR(32) NOT NULL DEFAULT '',
    notified_channels VARCHAR(64) NOT NULL DEFAULT '',
    context JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_IRRIGATION_DECISION_DDL = """
CREATE TABLE analytics_irrigationdecision (
    id BIGSERIAL PRIMARY KEY,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision_date DATE,
    user_id BIGINT NOT NULL,
    zone_id BIGINT NOT NULL REFERENCES analytics_zone (id) ON DELETE CASCADE,
    source VARCHAR(20) NOT NULL DEFAULT '',
    irrigate BOOLEAN NOT NULL,
    reason VARCHAR(32) NOT NULL,
    net_mm DOUBLE PRECISION NOT NULL DEFAULT 0,
    gross_mm DOUBLE PRECISION NOT NULL DEFAULT 0,
    volume_m3 DOUBLE PRECISION NOT NULL DEFAULT 0,
    duration_hr DOUBLE PRECISION NOT NULL DEFAULT 0,
    morning_volume_m3 DOUBLE PRECISION,
    evening_volume_m3 DOUBLE PRECISION,
    capped_to_daily_max BOOLEAN NOT NULL DEFAULT false,
    summary TEXT NOT NULL DEFAULT '',
    dr_today_mm DOUBLE PRECISION,
    raw_mm DOUBLE PRECISION,
    taw_mm DOUBLE PRECISION,
    et0_mm DOUBLE PRECISION,
    kc_used DOUBLE PRECISION,
    etc_mm DOUBLE PRECISION,
    soil_moisture_pct DOUBLE PRECISION,
    critical_moisture_pct DOUBLE PRECISION,
    precipitation_forecast_mm DOUBLE PRECISION NOT NULL DEFAULT 0,
    effective_rainfall_mm DOUBLE PRECISION,
    zone_area_m2 DOUBLE PRECISION,
    flow_rate_m3h DOUBLE PRECISION,
    max_water_per_day_m3 DOUBLE PRECISION,
    irrigation_efficiency DOUBLE PRECISION,
    kr DOUBLE PRECISION,
    context JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_HISTORY_DDL = (_ALERT_EVENT_DDL, _IRRIGATION_DECISION_DDL)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    schema_compat.reset_table_cache()
    yield
    schema_compat.reset_table_cache()


def _alert(**overrides):
    """An ``analytics_alert``-alike; the builders only read attributes."""
    values = {
        "id": 5,
        "name": "Humidité basse",
        "condition": "<",
        "condition_nbr": 30.0,
        "zone_id": None,
        "notification_zone_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ---------------------------------------------------------------------------
# Unit level — the alert-event row builder. No database.
# ---------------------------------------------------------------------------
def test_alert_event_row_snapshots_the_rule_as_it_fired():
    row = history.build_alert_event_row(
        alert=_alert(),
        user_id=10,
        sensor_key="soil_moisture",
        observed_value=12.5,
        reading_at=None,
        unit="%",
    )

    # the rule, copied — not referenced
    assert row["alert_name"] == "Humidité basse"
    assert row["condition"] == "<"
    assert row["threshold_value"] == 30.0
    assert row["unit"] == "%"
    assert row["observed_value"] == 12.5
    assert row["alert_id"] == 5


def test_alert_event_row_keys_match_the_insert_columns():
    row = history.build_alert_event_row(
        alert=_alert(),
        user_id=1,
        sensor_key="ph_soil",
        observed_value=8.0,
        reading_at=None,
    )
    assert set(row) == set(history.ALERT_EVENT_COLUMNS)


def test_alert_event_row_files_a_user_wide_rule_under_the_reading_zone():
    # A rule with no zone still fired ON a zone; the report filters by zone.
    row = history.build_alert_event_row(
        alert=_alert(zone_id=None),
        user_id=10,
        sensor_key="ph_soil",
        observed_value=8.0,
        reading_at=None,
        zone_id=20,
    )
    assert row["zone_id"] == 20


def test_alert_event_row_prefers_the_rules_own_zone():
    row = history.build_alert_event_row(
        alert=_alert(zone_id=99),
        user_id=10,
        sensor_key="ph_soil",
        observed_value=8.0,
        reading_at=None,
        zone_id=20,
    )
    assert row["zone_id"] == 99


def test_alert_event_row_records_every_notified_channel():
    row = history.build_alert_event_row(
        alert=_alert(),
        user_id=10,
        sensor_key="ph_soil",
        observed_value=8.0,
        reading_at=None,
        channels=["email", "sms"],
    )
    assert row["notified_channels"] == "email,sms"


def test_alert_event_row_clips_snapshot_fields_to_their_columns():
    # A 250-char rule name must not turn a firing into a 500.
    row = history.build_alert_event_row(
        alert=_alert(name="x" * 250, condition="<>"),
        user_id=10,
        sensor_key="y" * 90,
        observed_value=1.0,
        reading_at=None,
        unit="z" * 40,
    )
    assert len(row["alert_name"]) == 200
    assert len(row["condition"]) == 1
    assert len(row["sensor_key"]) == 64
    assert len(row["unit"]) == 32


def test_alert_event_row_serializes_context_as_json_text():
    row = history.build_alert_event_row(
        alert=_alert(),
        user_id=10,
        sensor_key="ph_soil",
        observed_value=8.0,
        reading_at=None,
        context={"zone_id": 20, "grace_seconds": 300},
    )
    assert '"grace_seconds": 300' in row["context"]


# ---------------------------------------------------------------------------
# Unit level — the irrigation-decision row builders. No database.
# ---------------------------------------------------------------------------
_ADVICE = {
    "recommendation": "irrigate",
    "reason": "Humidité du sol 12.0 % < seuil critique 20.0 %.",
    "soil_moisture_pct": 12.0,
    "critical_moisture_threshold": 20.0,
    "et0_mm": 4.2,
    "vpd_kpa": 1.1,
    "dr_today_mm": 18.0,
    "raw_mm": 15.0,
    "zone_name": "parcelle-1",
    "zone_area_m2": 1000.0,
    "estimated_water_m3": 12.0,
    "estimated_duration_min": 90.0,
    "morning_volume_m3": 6.0,
    "evening_volume_m3": 6.0,
    "decision_source": "field_snapshot_dr",
}


def test_decision_row_from_advice_maps_outcome_and_inputs():
    row = history.build_decision_row_from_advice(
        user_id=10,
        zone_id=20,
        source="assistant",
        advice=_ADVICE,
        decision_reason="stress",
    )

    assert row["irrigate"] is True
    assert row["reason"] == "stress"  # the machine value the report groups by
    assert row["summary"] == _ADVICE["reason"]  # the French sentence
    assert row["volume_m3"] == 12.0
    assert row["duration_hr"] == 1.5  # minutes → hours
    assert row["morning_volume_m3"] == 6.0
    assert row["dr_today_mm"] == 18.0
    assert row["raw_mm"] == 15.0
    assert row["et0_mm"] == 4.2
    assert row["soil_moisture_pct"] == 12.0
    assert row["critical_moisture_pct"] == 20.0
    assert row["zone_area_m2"] == 1000.0
    assert row["source"] == "assistant"
    assert row["decision_date"] == row["decided_at"].date()


def test_decision_row_from_advice_holds_when_not_irrigating():
    row = history.build_decision_row_from_advice(
        user_id=10,
        zone_id=20,
        source="proactive",
        advice={**_ADVICE, "recommendation": "hold"},
        decision_reason="no_stress",
    )
    assert row["irrigate"] is False
    assert row["reason"] == "no_stress"


def test_decision_row_from_advice_falls_back_to_the_recommendation():
    row = history.build_decision_row_from_advice(
        user_id=10,
        zone_id=20,
        source="assistant",
        advice={**_ADVICE, "recommendation": "unknown"},
    )
    assert row["reason"] == "unknown"


def test_decision_rows_never_leave_a_not_null_column_empty():
    sparse = history.build_decision_row_from_advice(
        user_id=10,
        zone_id=20,
        source="assistant",
        advice={},
    )
    for column in (
        "irrigate",
        "reason",
        "net_mm",
        "gross_mm",
        "volume_m3",
        "duration_hr",
        "capped_to_daily_max",
        "summary",
        "precipitation_forecast_mm",
    ):
        assert sparse[column] is not None


def test_decision_row_keys_match_the_insert_columns():
    from_advice = history.build_decision_row_from_advice(
        user_id=1, zone_id=2, source="assistant", advice=_ADVICE
    )
    from_snapshot = history.build_decision_row_from_snapshot(
        user_id=1, zone_id=2, source="periodic", snapshot={}
    )
    expected = set(history.IRRIGATION_DECISION_COLUMNS)
    assert set(from_advice) == expected
    assert set(from_snapshot) == expected


_SNAPSHOT = {
    "zone_name": "parcelle-1",
    "et0_today_mm": 5.0,
    "kc_used": 1.2,
    "soil_moisture_pct": 14.0,
    "dr_today_mm": 22.0,
    "raw_mm": 15.0,
    "taw_mm": 40.0,
    "decision_reason": "stress",
    "recommended_volume_m3": 9.0,
    "recommended_duration_min": 30.0,
    "morning_volume_m3": None,
    "evening_volume_m3": None,
    "irrigation_decision": "Irriguer 9 m³ ce matin.",
}


def test_decision_row_from_snapshot_maps_the_water_balance_inputs():
    row = history.build_decision_row_from_snapshot(
        user_id=10,
        zone_id=20,
        source="periodic",
        snapshot=_SNAPSHOT,
        precipitation_forecast_mm=2.5,
    )

    assert row["irrigate"] is True
    assert row["reason"] == "stress"
    assert row["summary"] == "Irriguer 9 m³ ce matin."
    assert row["volume_m3"] == 9.0
    assert row["duration_hr"] == 0.5
    assert row["taw_mm"] == 40.0
    assert row["et0_mm"] == 5.0
    assert row["kc_used"] == 1.2
    assert row["etc_mm"] == pytest.approx(6.0)  # ET0 × Kc
    assert row["precipitation_forecast_mm"] == 2.5


def test_decision_row_from_snapshot_holds_on_a_no_stress_reason():
    row = history.build_decision_row_from_snapshot(
        user_id=10,
        zone_id=20,
        source="periodic",
        snapshot={**_SNAPSHOT, "decision_reason": "rain_will_suffice"},
    )
    assert row["irrigate"] is False


def test_decision_row_from_snapshot_survives_an_empty_snapshot():
    row = history.build_decision_row_from_snapshot(
        user_id=10, zone_id=20, source="periodic", snapshot={}
    )
    assert row["irrigate"] is False
    assert row["reason"] == "unknown"
    assert row["etc_mm"] is None


def test_the_decision_source_label_is_an_allowlist():
    # ``params`` reaches the tool straight from the model's tool call.
    assert assistant_tools._decision_source({}) == "assistant"
    assert assistant_tools._decision_source({"_source": "proactive"}) == "proactive"
    assert assistant_tools._decision_source({"_source": "DROP TABLE"}) == "assistant"


# ---------------------------------------------------------------------------
# Unit level — the best-effort wrapper. This is the whole risk of the ticket.
# ---------------------------------------------------------------------------
class _Savepoint:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _StubSession:
    """Records savepoints and statements; can be told to fail on execute."""

    def __init__(self, *, boom: bool = False):
        self.savepoints: list[_Savepoint] = []
        self.statements: list = []
        self._boom = boom

    def begin_nested(self):
        sp = _Savepoint()
        self.savepoints.append(sp)
        return sp

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if self._boom:
            raise RuntimeError("history table exploded")
        return None

    def get_bind(self):  # pragma: no cover - only reached if a probe runs
        raise AssertionError("the probe must be stubbed in unit tests")


def test_best_effort_swallows_the_failure_and_logs_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="fastapp.history"):
        with history.best_effort("alert event"):
            raise RuntimeError("boom")
    assert "failed to record alert event" in caplog.text


def test_best_effort_rolls_the_savepoint_back_so_the_caller_survives():
    session = _StubSession()
    with history.best_effort("alert event", session=session):
        raise RuntimeError("boom")
    (savepoint,) = session.savepoints
    # rolled back, NOT committed: the caller's own statements stay valid
    assert (savepoint.rolled_back, savepoint.committed) == (True, False)


def test_best_effort_releases_the_savepoint_on_success():
    session = _StubSession()
    with history.best_effort("alert event", session=session):
        pass
    (savepoint,) = session.savepoints
    assert (savepoint.committed, savepoint.rolled_back) == (True, False)


def test_record_alert_event_writes_nothing_when_the_table_is_absent(monkeypatch):
    monkeypatch.setattr(history, "alert_events_available", lambda session: False)
    session = _StubSession()

    history.record_alert_event(
        session,
        alert=_alert(),
        user_id=10,
        sensor_key="ph_soil",
        observed_value=8.0,
        reading_at=None,
    )

    assert session.statements == []
    # the probe runs OUTSIDE the savepoint: nothing is opened at all
    assert session.savepoints == []


def test_record_alert_event_never_raises_when_the_insert_fails(monkeypatch, caplog):
    monkeypatch.setattr(history, "alert_events_available", lambda session: True)
    session = _StubSession(boom=True)

    with caplog.at_level(logging.WARNING, logger="fastapp.history"):
        history.record_alert_event(
            session,
            alert=_alert(),
            user_id=10,
            sensor_key="ph_soil",
            observed_value=8.0,
            reading_at=None,
        )

    assert len(session.statements) == 1
    assert session.savepoints[0].rolled_back is True
    assert "failed to record alert event" in caplog.text


def test_record_alert_event_never_raises_on_a_malformed_call(caplog):
    with caplog.at_level(logging.WARNING, logger="fastapp.history"):
        history.record_alert_event(_StubSession(), alert=_alert())  # missing kwargs
    assert "failed to record alert event" in caplog.text


def test_record_irrigation_decision_never_raises_when_the_insert_fails(
    monkeypatch, caplog
):
    monkeypatch.setattr(history, "irrigation_decisions_available", lambda session: True)
    session = _StubSession(boom=True)
    row = history.build_decision_row_from_advice(
        user_id=1, zone_id=2, source="assistant", advice=_ADVICE
    )

    with caplog.at_level(logging.WARNING, logger="fastapp.history"):
        history.record_irrigation_decision(session, row)

    assert "failed to record irrigation decision" in caplog.text


def test_record_advice_decision_never_raises_when_the_session_is_dead(
    monkeypatch, caplog
):
    @contextmanager
    def _broken(*args, **kwargs):
        raise RuntimeError("no database")
        yield  # pragma: no cover

    monkeypatch.setattr(history, "session_scope", _broken)

    with caplog.at_level(logging.WARNING, logger="fastapp.history"):
        history.record_advice_decision(
            user_id=1, zone_id=2, source="assistant", advice=_ADVICE
        )

    assert "failed to record irrigation decision" in caplog.text


# ---------------------------------------------------------------------------
# Unit level — the report endpoints' filter helpers.
# ---------------------------------------------------------------------------
def test_report_scope_filter_narrows_an_owner_to_one_zone():
    where: list[str] = []
    params: dict = {}
    expanding: list[str] = []

    ok = reports_router._scope_filter(
        _ReadScope(owner_id=10, zone_ids=None), 20, where, params, expanding
    )

    assert ok is True
    assert where == ["user_id = :owner", "zone_id = :zone"]
    assert params == {"owner": 10, "zone": 20}


def test_report_scope_filter_confines_a_technician_to_granted_zones():
    where: list[str] = []
    params: dict = {}
    expanding: list[str] = []

    ok = reports_router._scope_filter(
        _ReadScope(owner_id=10, zone_ids={20}, is_read_only=True),
        None,
        where,
        params,
        expanding,
    )

    assert ok is True
    assert params["owner"] == 10 and params["zones"] == [20]
    assert expanding == ["zones"]


def test_report_scope_filter_refuses_a_zone_outside_the_grant():
    assert (
        reports_router._scope_filter(
            _ReadScope(owner_id=10, zone_ids={20}, is_read_only=True),
            21,
            [],
            {},
            [],
        )
        is False
    )


def test_report_range_filter_is_inclusive_on_both_ends():
    where: list[str] = []
    params: dict = {}
    reports_router._range_filter(
        datetime.datetime(2026, 7, 1),
        datetime.datetime(2026, 7, 2),
        "triggered_at",
        where,
        params,
    )
    assert where == ["triggered_at >= :start", "triggered_at <= :end"]
    # a naive bound is read as UTC, like every stored stamp
    assert params["start"].tzinfo is datetime.timezone.utc


def test_report_page_size_is_capped():
    assert reports_router._page(None, None) == (reports_router.DEFAULT_LIMIT, 0)
    assert reports_router._page(10_000, -5) == (reports_router.MAX_LIMIT, 0)


# ---------------------------------------------------------------------------
# Integration level — real endpoints + real write paths against real Postgres,
# in BOTH schema shapes. The tables live in their own schema (search_path) so
# the Django test database is untouched.
# ---------------------------------------------------------------------------
_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="the history writers and /reports run against a real Postgres schema",
)

_SCHEMA = {False: "compat_no_history", True: "compat_history"}

_OWNER_ID = 40
_OTHER_ID = 41
_TECH_ID = 42
_ZONE_ID = 50
_ZONE2_ID = 51
_OTHER_ZONE_ID = 52
_ALERT_ID = 60


def _engine_for(schema: str):
    return create_engine(
        os.environ["AGRI_DB_URL"],
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={schema}"},
    )


def _build_schema(has_tables: bool):
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
            for ddl in _HISTORY_DDL:
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
    "VALUES (:id, :user_id, :name, 1000.0, 20.0, 50.0, 50.0, 50.0, 15.0, "
    " 5.0, 20000.0, 0.0)"
)

_ALERT_SQL = (
    "INSERT INTO analytics_alert "
    "(id, description, name, type, condition, condition_nbr, is_active, "
    " sensor_key, notify_email, notify_whatsapp, notify_sms, user_id, zone_id) "
    "VALUES (:id, 'desc', :name, 'sensor', :condition, :threshold, true, "
    " :sensor_key, true, false, false, :user_id, :zone_id)"
)


def _seed(engine, has_tables: bool):
    """A farm (owner 40, zones 50/51) + a second farm (41/52) whose history the
    owner must never reach, plus one active pH alert on zone 50."""
    tables = [
        "analytics_phsoil",
        "analytics_soilmoisturemedium",
        "analytics_alert",
        "analytics_technicianzonegrant",
        "analytics_techniciangrant",
        "analytics_device",
        "analytics_zone",
        '"CustomUser_customuser"',
    ]
    if has_tables:
        tables = ["analytics_alertevent", "analytics_irrigationdecision"] + tables
    with Session(engine) as session:
        session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        for uid, username, is_tech in (
            (_OWNER_ID, "hist-owner", False),
            (_OTHER_ID, "hist-other", False),
            (_TECH_ID, "hist-tech", True),
        ):
            session.execute(
                text(_USER_SQL),
                {
                    "id": uid,
                    "username": username,
                    "email": f"{username}@example.com",
                    "is_technician": is_tech,
                },
            )
        for zid, uid, name in (
            (_ZONE_ID, _OWNER_ID, "parcelle-1"),
            (_ZONE2_ID, _OWNER_ID, "parcelle-2"),
            (_OTHER_ZONE_ID, _OTHER_ID, "parcelle-voisine"),
        ):
            session.execute(text(_ZONE_SQL), {"id": zid, "user_id": uid, "name": name})
        session.execute(
            text(_ALERT_SQL),
            {
                "id": _ALERT_ID,
                "name": "pH trop haut",
                "condition": ">",
                "threshold": 7.0,
                "sensor_key": "ph_soil",
                "user_id": _OWNER_ID,
                "zone_id": _ZONE_ID,
            },
        )
        session.commit()


def _scope_factory(engine):
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

    return _scope


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
def history_api(request, _schemas, monkeypatch):
    """``(client, has_tables, engine, enqueued)`` — a TestClient whose ingest,
    assistant and report paths all run against the schema shape under test.
    ``enqueued`` collects the Celery tasks ingest would have sent."""
    has_tables = request.param
    engine = _schemas[has_tables]
    scope = _scope_factory(engine)

    for module in (reports_router, ingest_router, assistant_tools, history):
        monkeypatch.setattr(module, "session_scope", scope)

    enqueued: list[tuple] = []
    monkeypatch.setattr(
        fastapp_celery,
        "send_task",
        lambda name, **kwargs: enqueued.append((name, kwargs)),
    )

    _seed(engine, has_tables)
    _authenticate_as(_OWNER_ID, "hist-owner")
    try:
        with TestClient(app) as client:
            yield client, has_tables, engine, enqueued
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _ingest_ph(client, value: float, *, client_name: str = "hist-owner"):
    return client.post(
        "/ingest/sensor",
        json={"client": client_name, "sensor_key": "ph_soil", "value": value},
    )


def _events(engine) -> list:
    with Session(engine) as session:
        return session.execute(
            text("SELECT * FROM analytics_alertevent ORDER BY id")
        ).all()


def _decisions(engine) -> list:
    with Session(engine) as session:
        return session.execute(
            text("SELECT * FROM analytics_irrigationdecision ORDER BY id")
        ).all()


# --- the alert write path ---------------------------------------------------
@_requires_pg
def test_a_firing_alert_is_recorded_with_the_rule_snapshot(history_api):
    client, has_tables, engine, enqueued = history_api

    resp = _ingest_ph(client, 8.4)

    # the ingest itself always succeeds and always sends the alert
    assert resp.status_code == 201, resp.text
    assert [name for name, _ in enqueued] == ["agriapi.tasks.send_alert_email"]
    if not has_tables:
        return

    (event,) = _events(engine)
    assert event.alert_id == _ALERT_ID
    assert event.user_id == _OWNER_ID
    assert event.zone_id == _ZONE_ID
    assert event.sensor_key == "ph_soil"
    assert event.alert_name == "pH trop haut"
    assert event.condition == ">"
    assert event.threshold_value == 7.0
    assert event.observed_value == 8.4
    assert event.notified_channels == "email"
    assert event.unit  # snapshotted from the sensor registry


@_requires_pg
def test_editing_the_rule_afterwards_does_not_rewrite_history(history_api):
    client, has_tables, engine, _ = history_api
    _ingest_ph(client, 8.4)
    if not has_tables:
        return

    with Session(engine) as session:
        session.execute(
            text(
                "UPDATE analytics_alert SET name = 'renommée', condition = '<', "
                "condition_nbr = 3 WHERE id = :id"
            ),
            {"id": _ALERT_ID},
        )
        session.commit()

    (event,) = _events(engine)
    assert (event.alert_name, event.condition, event.threshold_value) == (
        "pH trop haut",
        ">",
        7.0,
    )


@_requires_pg
def test_deleting_the_rule_keeps_the_firing_readable(history_api):
    client, has_tables, engine, _ = history_api
    _ingest_ph(client, 8.4)
    if not has_tables:
        return

    with Session(engine) as session:
        session.execute(
            text("DELETE FROM analytics_alert WHERE id = :id"), {"id": _ALERT_ID}
        )
        session.commit()

    (event,) = _events(engine)
    assert event.alert_id is None  # FK ON DELETE SET NULL
    assert event.alert_name == "pH trop haut"  # the snapshot survives


@_requires_pg
def test_a_reading_below_the_threshold_records_nothing(history_api):
    client, has_tables, engine, enqueued = history_api

    assert _ingest_ph(client, 6.0).status_code == 201
    assert enqueued == []
    if has_tables:
        assert _events(engine) == []


@_requires_pg
def test_ingest_survives_a_broken_history_insert(history_api, monkeypatch, caplog):
    """The best-effort contract, stated as a test: a history INSERT that dies
    must cost the reading nothing and must not stop the alert.

    WITHOUT the wrapper the failure aborts the ingest transaction — Postgres
    refuses every later statement in it — so the READING is rolled back too.
    """
    client, has_tables, engine, enqueued = history_api
    if not has_tables:
        pytest.skip("needs the tables to break the INSERT against them")

    broken = text("INSERT INTO analytics_alertevent (user_id) VALUES (NULL)")
    monkeypatch.setattr(history, "_INSERT_ALERT_EVENT", broken)

    with caplog.at_level(logging.WARNING, logger="fastapp.history"):
        resp = _ingest_ph(client, 8.4)

    assert resp.status_code == 201, resp.text
    assert [name for name, _ in enqueued] == ["agriapi.tasks.send_alert_email"]
    assert "failed to record alert event" in caplog.text
    assert _events(engine) == []
    # the reading itself was still committed
    with Session(engine) as session:
        assert (
            session.execute(text("SELECT count(*) FROM analytics_phsoil")).scalar_one()
            == 1
        )


# --- the irrigation-decision write path ------------------------------------
def _seed_soil_moisture(engine, value: float):
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO analytics_soilmoisturemedium "
                "(user_id, zone_id, value, timestamp) "
                "VALUES (:u, :z, :v, now())"
            ),
            {"u": _OWNER_ID, "z": _ZONE_ID, "v": value},
        )
        session.commit()


def _advise(source: str = "assistant") -> dict:
    return assistant_tools._get_irrigation_advice(
        AuthedUser(
            id=_OWNER_ID,
            username="hist-owner",
            email="hist-owner@example.com",
            is_staff=False,
            is_technician=False,
            preferred_language="fr",
        ),
        {"_source": source},
    )


@_requires_pg
def test_an_irrigation_recommendation_is_recorded_with_its_inputs(history_api):
    _client, has_tables, engine, _ = history_api
    _seed_soil_moisture(engine, 12.0)  # below the zone's 20 % critical threshold

    advice = _advise()

    # the recommendation is returned either way — that is the contract
    assert advice["recommendation"] == "irrigate"
    if not has_tables:
        return

    (decision,) = _decisions(engine)
    assert decision.user_id == _OWNER_ID
    assert decision.zone_id == _ZONE_ID
    assert decision.source == "assistant"
    assert decision.irrigate is True
    assert decision.summary  # the French sentence the farmer saw
    assert decision.soil_moisture_pct == 12.0
    assert decision.critical_moisture_pct == 20.0
    assert decision.decision_date is not None


@_requires_pg
def test_the_proactive_scan_labels_its_own_decisions(history_api):
    _client, has_tables, engine, _ = history_api
    _seed_soil_moisture(engine, 12.0)

    _advise("proactive")

    if has_tables:
        (decision,) = _decisions(engine)
        assert decision.source == "proactive"


@_requires_pg
def test_a_recommendation_still_answers_when_the_decision_insert_fails(
    history_api, monkeypatch, caplog
):
    _client, has_tables, engine, _ = history_api
    if not has_tables:
        pytest.skip("needs the tables to break the INSERT against them")
    _seed_soil_moisture(engine, 12.0)
    monkeypatch.setattr(
        history,
        "_INSERT_IRRIGATION_DECISION",
        text("INSERT INTO analytics_irrigationdecision (user_id) VALUES (NULL)"),
    )

    with caplog.at_level(logging.WARNING, logger="fastapp.history"):
        advice = _advise()

    assert advice["recommendation"] == "irrigate"
    assert "failed to record irrigation decision" in caplog.text
    assert _decisions(engine) == []


# --- the report endpoints ---------------------------------------------------
def _insert_event(engine, *, zone_id: int, user_id: int, when: str, value: float):
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO analytics_alertevent "
                "(triggered_at, alert_id, user_id, zone_id, sensor_key, "
                " alert_name, condition, threshold_value, observed_value, unit) "
                "VALUES (:when, NULL, :user_id, :zone_id, 'ph_soil', 'pH', '>', "
                " 7.0, :value, 'pH')"
            ),
            {
                "when": when,
                "user_id": user_id,
                "zone_id": zone_id,
                "value": value,
            },
        )
        session.commit()


def _insert_decision(engine, *, zone_id: int, user_id: int, when: str):
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO analytics_irrigationdecision "
                "(decided_at, decision_date, user_id, zone_id, source, irrigate, "
                " reason, volume_m3) "
                "VALUES (CAST(:when AS TIMESTAMPTZ), CAST(:when AS DATE), "
                " :user_id, :zone_id, 'periodic', true, "
                " 'stress', 4.0)"
            ),
            {"when": when, "user_id": user_id, "zone_id": zone_id},
        )
        session.commit()


def _seed_report_rows(engine):
    _insert_event(
        engine,
        zone_id=_ZONE_ID,
        user_id=_OWNER_ID,
        when="2026-07-01T10:00:00Z",
        value=8.0,
    )
    _insert_event(
        engine,
        zone_id=_ZONE2_ID,
        user_id=_OWNER_ID,
        when="2026-07-05T10:00:00Z",
        value=9.0,
    )
    _insert_event(
        engine,
        zone_id=_OTHER_ZONE_ID,
        user_id=_OTHER_ID,
        when="2026-07-05T11:00:00Z",
        value=10.0,
    )
    _insert_decision(
        engine, zone_id=_ZONE_ID, user_id=_OWNER_ID, when="2026-07-01T06:00:00Z"
    )
    _insert_decision(
        engine, zone_id=_ZONE2_ID, user_id=_OWNER_ID, when="2026-07-05T06:00:00Z"
    )
    _insert_decision(
        engine, zone_id=_OTHER_ZONE_ID, user_id=_OTHER_ID, when="2026-07-05T06:00:00Z"
    )


@_requires_pg
def test_reports_answer_empty_without_the_tables(history_api):
    client, has_tables, _engine, _ = history_api
    if has_tables:
        pytest.skip("this is the degraded shape")

    for path in ("/reports/alert-events", "/reports/irrigation-decisions"):
        resp = client.get(path)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["results"] == [] and body["count"] == 0
        assert body["schema_available"] is False


@_requires_pg
def test_alert_event_report_lists_only_the_callers_firings(history_api):
    client, has_tables, engine, _ = history_api
    if not has_tables:
        pytest.skip("covered by test_reports_answer_empty_without_the_tables")
    _seed_report_rows(engine)

    body = client.get("/reports/alert-events").json()

    assert body["count"] == 2  # the neighbour's firing is not visible
    assert {r["zone_id"] for r in body["results"]} == {_ZONE_ID, _ZONE2_ID}
    assert body["results"][0]["triggered_at"] > body["results"][1]["triggered_at"]
    assert body["results"][0]["alert_name"] == "pH"


@_requires_pg
def test_alert_event_report_filters_by_date_range_and_zone(history_api):
    client, has_tables, engine, _ = history_api
    if not has_tables:
        pytest.skip("covered by test_reports_answer_empty_without_the_tables")
    _seed_report_rows(engine)

    ranged = client.get(
        "/reports/alert-events",
        params={"start": "2026-07-04T00:00:00Z", "end": "2026-07-06T00:00:00Z"},
    ).json()
    assert [r["zone_id"] for r in ranged["results"]] == [_ZONE2_ID]

    zoned = client.get("/reports/alert-events", params={"zone_id": _ZONE_ID}).json()
    assert [r["zone_id"] for r in zoned["results"]] == [_ZONE_ID]

    both = client.get(
        "/reports/alert-events",
        params={"zone_id": _ZONE_ID, "start": "2026-07-04T00:00:00Z"},
    ).json()
    assert both["results"] == [] and both["count"] == 0


@_requires_pg
def test_alert_event_report_refuses_a_foreign_zone(history_api):
    client, has_tables, engine, _ = history_api
    if not has_tables:
        pytest.skip("covered by test_reports_answer_empty_without_the_tables")
    _seed_report_rows(engine)

    body = client.get(
        "/reports/alert-events", params={"zone_id": _OTHER_ZONE_ID}
    ).json()

    assert body["results"] == [] and body["count"] == 0


@_requires_pg
def test_irrigation_report_filters_by_date_range_and_zone(history_api):
    client, has_tables, engine, _ = history_api
    if not has_tables:
        pytest.skip("covered by test_reports_answer_empty_without_the_tables")
    _seed_report_rows(engine)

    everything = client.get("/reports/irrigation-decisions").json()
    assert everything["count"] == 2  # never the neighbour's

    ranged = client.get(
        "/reports/irrigation-decisions",
        params={"start": "2026-07-04T00:00:00Z", "zone_id": _ZONE2_ID},
    ).json()
    (decision,) = ranged["results"]
    assert decision["zone_id"] == _ZONE2_ID
    assert decision["irrigate"] is True
    assert decision["reason"] == "stress"
    assert decision["volume_m3"] == 4.0
    assert "dr_today_mm" in decision["inputs"]


@_requires_pg
def test_irrigation_report_paginates(history_api):
    client, has_tables, engine, _ = history_api
    if not has_tables:
        pytest.skip("covered by test_reports_answer_empty_without_the_tables")
    _seed_report_rows(engine)

    page = client.get(
        "/reports/irrigation-decisions", params={"limit": 1, "offset": 1}
    ).json()

    assert page["count"] == 2  # the total, not the page
    assert len(page["results"]) == 1
    assert page["limit"] == 1 and page["offset"] == 1


@_requires_pg
def test_a_technician_sees_only_the_granted_zones(history_api):
    client, has_tables, engine, _ = history_api
    if not has_tables:
        pytest.skip("covered by test_reports_answer_empty_without_the_tables")
    _seed_report_rows(engine)
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO analytics_techniciangrant "
                "(id, owner_id, technician_id, is_active, created_at) "
                "VALUES (1, :owner, :tech, true, now())"
            ),
            {"owner": _OWNER_ID, "tech": _TECH_ID},
        )
        session.execute(
            text(
                "INSERT INTO analytics_technicianzonegrant "
                "(id, grant_id, zone_id, allowed_graphs) "
                "VALUES (1, 1, :zone, '{}')"
            ),
            {"zone": _ZONE_ID},
        )
        session.commit()
    _authenticate_as(_TECH_ID, "hist-tech", is_technician=True)

    body = client.get("/reports/alert-events").json()

    assert [r["zone_id"] for r in body["results"]] == [_ZONE_ID]
