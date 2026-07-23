"""Per-sensor calibration APPLICATION (#446) — the single read-path choke point.

The editor (#439) stores ``analytics_sensorcalibration`` rows; this suite proves
they are finally APPLIED, and — the whole point of the ticket — applied at ONE
place so the dashboard, the alert evaluator and the report history read the
SAME corrected number.

Two levels:

* **unit** — :func:`fastapp.calibration.corrected_value` (active / inactive /
  absent / None / unit-conversion / bad-unit fallback) and the pure guards of
  :func:`fastapp.calibration.load_calibrations`. No database.
* **integration** — the REAL ``/sensors/<slug>`` endpoint AND the REAL alert
  dispatch (:func:`fastapp.ingest.dispatch_alerts_for_reading`) over one Postgres
  schema, asserting a calibrated reading comes back corrected from the chart AND
  is evaluated + recorded corrected by the alert path — the exact same value.

**What regresses if only ONE surface applied calibration:** the dashboard would
plot 6.5 pH while the alert still fired on the stored 6.0 (or vice-versa), and
the alert-event report would record a third number. #67's acceptance —
"corrected values are used consistently across dashboard, alerts and reports" —
would be violated: the farmer would see a chart that contradicts the alert that
contradicts the report. The consistency test below fails the instant any one
surface stops going through the shared helper.
"""

from __future__ import annotations

import datetime
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
import agri.db.technicians  # noqa: F401
import agri.db.users  # noqa: F401
from agri.core.calibration import Calibration
from agri.db.base import AgriBase
from fastapp import calibration as calib
from fastapp import ingest, schema_compat, sensors
from fastapp.auth import AuthedUser, get_current_user
from fastapp.calibration import corrected_value, load_calibrations
from fastapp.main import app

# ---------------------------------------------------------------------------
# Unit level — corrected_value. No Postgres.
# ---------------------------------------------------------------------------


def test_corrected_value_applies_an_active_calibration():
    cal = Calibration(scale_a=2.0, offset_b=1.0)
    assert corrected_value(10.0, cal) == 21.0


def test_corrected_value_skips_an_inactive_calibration():
    cal = Calibration(scale_a=2.0, offset_b=1.0, is_active=False)
    # disabled, not deleted → the raw value is returned untouched
    assert corrected_value(10.0, cal) == 10.0


def test_corrected_value_returns_raw_when_no_calibration_exists():
    assert corrected_value(10.0, None) == 10.0


def test_corrected_value_keeps_a_missing_reading_missing():
    assert corrected_value(None, Calibration(scale_a=2.0)) is None


def test_corrected_value_converts_units_when_the_target_differs():
    # A calibration expressed in °F, read back in the sensor's native °C: the
    # affine step is identity, the conversion is 50 °F → 10 °C.
    cal = Calibration(scale_a=1.0, offset_b=0.0, unit="°F")
    got = corrected_value(50.0, cal, sensor_key="temperature_weather", native_unit="°C")
    assert got == pytest.approx(10.0)


def test_corrected_value_falls_back_to_affine_on_an_impossible_conversion():
    # A pH sensor mis-calibrated in °C: agri-core would raise on °C → pH. Rather
    # than 500 the chart / drop the alert, the affine correction applies alone.
    cal = Calibration(scale_a=2.0, offset_b=0.0, unit="°C")
    assert corrected_value(3.0, cal, sensor_key=None, native_unit="pH") == 6.0


def test_load_calibrations_ignores_null_device_and_blank_key():
    class _Boom:
        def execute(self, *a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("no query should run for un-keyable pairs")

    # every pair is un-keyable → an empty map without ever touching the session
    assert load_calibrations(_Boom(), [(None, "ph_soil"), (5, ""), (None, None)]) == {}


def test_load_calibrations_is_empty_when_the_table_is_absent(monkeypatch):
    monkeypatch.setattr(calib, "sensor_calibration_available", lambda _s: False)
    # a real pair, but the deployment has no calibration table → nothing applied
    assert load_calibrations(object(), [(30, "ph_soil")]) == {}


# ---------------------------------------------------------------------------
# Integration level — the real endpoint + the real alert path over Postgres.
# The tables live in their own schema (search_path) so the Django test DB is
# untouched; the routers' unqualified SQL resolves there exactly as it resolves
# to ``public`` in production.
# ---------------------------------------------------------------------------
_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="calibration application is exercised against a real Postgres schema",
)

_SCHEMA = "calibration_application"
_OWNER_ID = 10
_OTHER_ID = 11
_ZONE_ID = 20
_OTHER_ZONE_ID = 21
_DEVICE_ID = 30
_DEVICE_2_ID = 31
_SENSOR_KEY = "ph_soil"
_SLUG = "phsoil"
_HOUR = datetime.datetime(2026, 6, 1, 9, tzinfo=datetime.timezone.utc)

_USER_SQL = (
    'INSERT INTO "CustomUser_customuser" '
    "(id, password, is_superuser, username, firstname, lastname, email, "
    " payement_status, is_active, is_staff, is_technician, notify_every, "
    " preferred_language, date_joined) "
    "VALUES (:id, '', false, :username, 'F', 'L', :email, 'actif', true, false, "
    " false, 240, 'fr', now())"
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
_READING_SQL = (
    "INSERT INTO analytics_phsoil (timestamp, user_id, zone_id, value, device_id) "
    "VALUES (:ts, :user_id, :zone_id, :value, :device_id)"
)
_CALIB_SQL = (
    "INSERT INTO analytics_sensorcalibration "
    "(device_id, sensor_key, scale_a, offset_b, unit, is_active, note, "
    " created_at, updated_at) "
    "VALUES (:device_id, :key, :scale_a, :offset_b, :unit, :is_active, '', "
    " now(), now())"
)
_ALERT_SQL = (
    "INSERT INTO analytics_alert "
    "(id, description, name, type, condition, condition_nbr, is_active, "
    " sensor_key, notify_email, notify_whatsapp, notify_sms, user_id, zone_id, "
    " created_at) "
    "VALUES (:id, '', :name, 'ph', :condition, :threshold, true, :key, "
    " true, false, false, :user_id, :zone_id, now())"
)


@pytest.fixture(scope="module")
def _engine(django_db_setup):
    if not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"):
        pytest.skip("requires Postgres")
    admin = create_engine(os.environ["AGRI_DB_URL"], poolclass=NullPool)
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    admin.dispose()

    engine = create_engine(
        os.environ["AGRI_DB_URL"],
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={_SCHEMA}"},
    )
    meta = MetaData(schema=_SCHEMA)
    for table in AgriBase.metadata.sorted_tables:
        table.to_metadata(meta, schema=_SCHEMA)
    meta.create_all(engine)
    yield engine
    engine.dispose()
    admin = create_engine(os.environ["AGRI_DB_URL"], poolclass=NullPool)
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
    admin.dispose()


def _seed(engine, *, readings, calibrations=(), alerts=()):
    """Reset the schema to two farms, then load the given readings / calibrations
    / alerts. ``readings`` = list of (value, device_id); device 30 and 31 belong
    to owner 10, device 31 sits in a second zone so per-device keying is real."""
    with Session(engine) as s:
        s.execute(
            text(
                "TRUNCATE analytics_alertevent, analytics_alert, "
                "analytics_sensorcalibration, analytics_phsoil, analytics_device, "
                'analytics_zone, "CustomUser_customuser" RESTART IDENTITY CASCADE'
            )
        )
        for uid, uname in ((_OWNER_ID, "cal-owner"), (_OTHER_ID, "cal-other")):
            s.execute(
                text(_USER_SQL),
                {"id": uid, "username": uname, "email": f"{uname}@example.com"},
            )
        s.execute(text(_ZONE_SQL), {"id": _ZONE_ID, "user_id": _OWNER_ID, "name": "z1"})
        s.execute(
            text(_ZONE_SQL),
            {"id": _OTHER_ZONE_ID, "user_id": _OWNER_ID, "name": "z2"},
        )
        s.execute(
            text(_DEVICE_SQL),
            {
                "id": _DEVICE_ID,
                "user_id": _OWNER_ID,
                "zone_id": _ZONE_ID,
                "serial": "D-30",
                "name": "Device 30",
            },
        )
        s.execute(
            text(_DEVICE_SQL),
            {
                "id": _DEVICE_2_ID,
                "user_id": _OWNER_ID,
                "zone_id": _OTHER_ZONE_ID,
                "serial": "D-31",
                "name": "Device 31",
            },
        )
        for i, (value, device_id) in enumerate(readings):
            dev = s.execute(
                text("SELECT zone_id FROM analytics_device WHERE id = :d"),
                {"d": device_id},
            ).scalar_one()
            s.execute(
                text(_READING_SQL),
                {
                    "ts": _HOUR + datetime.timedelta(minutes=5 + i),
                    "user_id": _OWNER_ID,
                    "zone_id": dev,
                    "value": value,
                    "device_id": device_id,
                },
            )
        for c in calibrations:
            s.execute(text(_CALIB_SQL), c)
        for a in alerts:
            s.execute(text(_ALERT_SQL), a)
        s.commit()


@contextmanager
def _scope_factory(engine, *, commit: bool = False):
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
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


@pytest.fixture
def client(_engine, monkeypatch):
    """A TestClient whose /sensors reads resolve to the test schema, as owner 10."""
    schema_compat.reset_table_cache()

    def _scope(*, commit: bool = False):
        return _scope_factory(_engine, commit=commit)

    monkeypatch.setattr(sensors, "session_scope", _scope)
    app.dependency_overrides[get_current_user] = lambda: AuthedUser(
        id=_OWNER_ID,
        username="cal-owner",
        email="cal-owner@example.com",
        is_staff=False,
        is_technician=False,
        preferred_language="fr",
        access_level="admin",
    )
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        schema_compat.reset_table_cache()


def _calib(device_id, *, scale_a=1.0, offset_b=0.0, unit="", is_active=True):
    return {
        "device_id": device_id,
        "key": _SENSOR_KEY,
        "scale_a": scale_a,
        "offset_b": offset_b,
        "unit": unit,
        "is_active": is_active,
    }


@_requires_pg
def test_read_endpoint_returns_the_corrected_value(client, _engine):
    _seed(
        _engine,
        readings=[(6.0, _DEVICE_ID)],
        calibrations=[_calib(_DEVICE_ID, offset_b=0.5)],
    )
    # raw=true → the single stored row, corrected 6.0 → 6.5
    raw = client.get(f"/sensors/{_SLUG}?raw=true").json()
    assert [r["value"] for r in raw] == [pytest.approx(6.5)]
    # hourly average of a single row is that row, corrected the same way
    hourly = client.get(f"/sensors/{_SLUG}").json()
    assert [r["value"] for r in hourly] == [pytest.approx(6.5)]


@_requires_pg
def test_read_endpoint_ignores_an_inactive_calibration(client, _engine):
    _seed(
        _engine,
        readings=[(6.0, _DEVICE_ID)],
        calibrations=[_calib(_DEVICE_ID, offset_b=0.5, is_active=False)],
    )
    raw = client.get(f"/sensors/{_SLUG}?raw=true").json()
    assert [r["value"] for r in raw] == [pytest.approx(6.0)]


@_requires_pg
def test_read_endpoint_returns_raw_without_a_calibration(client, _engine):
    _seed(_engine, readings=[(6.0, _DEVICE_ID)])  # no calibration row at all
    raw = client.get(f"/sensors/{_SLUG}?raw=true").json()
    assert [r["value"] for r in raw] == [pytest.approx(6.0)]


@_requires_pg
def test_batch_load_returns_the_right_calibration_per_pair(_engine):
    _seed(
        _engine,
        readings=[(6.0, _DEVICE_ID), (6.0, _DEVICE_2_ID)],
        calibrations=[
            _calib(_DEVICE_ID, offset_b=0.5),
            _calib(_DEVICE_2_ID, scale_a=2.0),
        ],
    )
    schema_compat.reset_table_cache()
    with Session(_engine) as session:
        loaded = load_calibrations(
            session,
            [
                (_DEVICE_ID, _SENSOR_KEY),
                (_DEVICE_2_ID, _SENSOR_KEY),
                (_DEVICE_ID, "some_other_key"),  # no row → absent
            ],
        )
    assert loaded[(_DEVICE_ID, _SENSOR_KEY)].offset_b == 0.5
    assert loaded[(_DEVICE_ID, _SENSOR_KEY)].scale_a == 1.0
    assert loaded[(_DEVICE_2_ID, _SENSOR_KEY)].scale_a == 2.0
    assert (_DEVICE_ID, "some_other_key") not in loaded
    schema_compat.reset_table_cache()


@_requires_pg
def test_alert_and_read_agree_on_the_corrected_value(client, _engine, monkeypatch):
    """The consistency guarantee: one calibrated reading, corrected 6.0 → 6.5.

    The threshold 6.2 sits BETWEEN the raw and the corrected value, so the alert
    fires ONLY because the correction was applied — and the recorded observed
    value equals what the chart shows. The same number in all three surfaces.
    """
    _seed(
        _engine,
        readings=[(6.0, _DEVICE_ID)],
        calibrations=[_calib(_DEVICE_ID, offset_b=0.5)],
        alerts=[
            {
                "id": 1,
                "name": "pH high",
                "condition": ">",
                "threshold": 6.2,
                "key": _SENSOR_KEY,
                "user_id": _OWNER_ID,
                "zone_id": _ZONE_ID,
            }
        ],
    )
    # what the dashboard shows
    read_value = client.get(f"/sensors/{_SLUG}?raw=true").json()[0]["value"]

    # the alert path, given the RAW 6.0, must correct to 6.5 before evaluating
    sent: list[tuple] = []
    monkeypatch.setattr(
        ingest.celery, "send_task", lambda name, **kw: sent.append((name, kw))
    )
    schema_compat.reset_table_cache()
    with Session(_engine) as session:
        enqueued = ingest.dispatch_alerts_for_reading(
            session,
            sensor_key=_SENSOR_KEY,
            zone_id=_ZONE_ID,
            user_id=_OWNER_ID,
            value=6.0,
            timestamp=_HOUR,
            device_id=_DEVICE_ID,
        )
        session.commit()
        recorded = session.execute(
            text(
                "SELECT observed_value FROM analytics_alertevent "
                "WHERE alert_id = 1 ORDER BY id DESC LIMIT 1"
            )
        ).scalar_one()
    schema_compat.reset_table_cache()

    # fired only because 6.5 > 6.2 (raw 6.0 would NOT have)
    assert enqueued == 1
    assert sent and sent[0][0] == "agriapi.tasks.send_alert_email"
    # THE guarantee: dashboard value == recorded alert value == corrected value
    assert read_value == pytest.approx(6.5)
    assert recorded == pytest.approx(6.5)
    assert recorded == pytest.approx(read_value)


@_requires_pg
def test_alert_uses_raw_when_the_calibration_is_inactive(_engine, monkeypatch):
    """Inactive calibration → the alert sees the raw 6.0, which is below 6.2, so
    nothing fires. The mirror of the read path ignoring an inactive factor."""
    _seed(
        _engine,
        readings=[(6.0, _DEVICE_ID)],
        calibrations=[_calib(_DEVICE_ID, offset_b=0.5, is_active=False)],
        alerts=[
            {
                "id": 1,
                "name": "pH high",
                "condition": ">",
                "threshold": 6.2,
                "key": _SENSOR_KEY,
                "user_id": _OWNER_ID,
                "zone_id": _ZONE_ID,
            }
        ],
    )
    monkeypatch.setattr(ingest.celery, "send_task", lambda name, **kw: None)
    schema_compat.reset_table_cache()
    with Session(_engine) as session:
        enqueued = ingest.dispatch_alerts_for_reading(
            session,
            sensor_key=_SENSOR_KEY,
            zone_id=_ZONE_ID,
            user_id=_OWNER_ID,
            value=6.0,
            timestamp=_HOUR,
            device_id=_DEVICE_ID,
        )
    schema_compat.reset_table_cache()
    assert enqueued == 0
