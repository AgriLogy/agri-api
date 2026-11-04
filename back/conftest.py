"""Shared test fixtures for the django-ninja API surface.

Post-#116 the admin / backoffice endpoints are django-ninja routes that
authenticate via ``JwtAuth(HttpBearer)`` over simplejwt — NOT DRF session
auth. So tests must send ``Authorization: Bearer <access>`` instead of
``force_authenticate``. These fixtures mint a simplejwt access token for the
matching user fixture (``admin_user`` / ``normal_user`` / ``other_user``,
provided by the per-app conftests) and attach it to every request.
"""

from __future__ import annotations

import os

import pytest
from rest_framework.test import APIClient


@pytest.fixture(scope="session", autouse=True)
def _bind_agri_core_db(django_db_setup):
    """Point agri.core.database (``AGRI_DB_URL``) at Django's *test* database
    so the SQLAlchemy handlers and the Django ORM share one Postgres.

    No-op on sqlite — the dual-ORM handler tests skip without Postgres. Runs
    after ``django_db_setup`` so the connection's NAME is the test DB.
    """
    from django.db import connection

    sd = connection.settings_dict
    if sd["ENGINE"].endswith("postgresql"):
        os.environ["AGRI_DB_URL"] = (
            "postgresql+psycopg://"
            f"{sd['USER']}:{sd['PASSWORD']}@{sd['HOST']}:{sd['PORT']}/{sd['NAME']}"
        )
        from agri.core import database as agri_db

        agri_db.dispose_engine()
    yield


@pytest.fixture(scope="session", autouse=True)
def _create_assistant_history_table(django_db_setup, django_db_blocker):
    """``assistant_conversation`` is created out-of-band in prod (homeless
    model, not migrate-managed). Create it once for the whole test DB so the
    flush-based TransactionTestCases across all apps don't choke on a missing
    table.
    """
    from django.db import connection

    from apps.assistant.models import AssistantConversation, ProactiveNotice

    with django_db_blocker.unblock():
        existing = set(connection.introspection.table_names())
        for model in (AssistantConversation, ProactiveNotice):
            if model._meta.db_table not in existing:
                with connection.schema_editor() as editor:
                    editor.create_model(model)
    yield


@pytest.fixture(scope="session", autouse=True)
def _create_technician_tables(django_db_setup, django_db_blocker):
    """``analytics_techniciangrant`` + ``analytics_technicianzonegrant`` are
    unmanaged (self-deployed in prod via scripts/ensure_technician_tables.py).
    Create them once for the whole test DB so flush-based TransactionTestCases
    across all apps don't choke on a missing table.
    """
    from django.db import connection

    from apps.irrigation.models import TechnicianGrant, TechnicianZoneGrant

    with django_db_blocker.unblock():
        existing = connection.introspection.table_names()
        with connection.schema_editor() as editor:
            for model in (TechnicianGrant, TechnicianZoneGrant):
                if model._meta.db_table not in existing:
                    editor.create_model(model)
    yield


@pytest.fixture(scope="session", autouse=True)
def _create_sector_schema(django_db_setup, django_db_blocker):
    """``analytics_sector`` is unmanaged and ``analytics_zone.sector_id`` is
    added out-of-band in prod (scripts/ensure_sector_tables.py, Alembic
    b33c23723140). Create the table + column once for the test DB so zone
    inserts don't choke on the missing ``sector_id``.
    """
    from django.db import connection

    from apps.irrigation.models import Sector, Zone

    with django_db_blocker.unblock():
        existing = set(connection.introspection.table_names())
        if Sector._meta.db_table not in existing:
            with connection.schema_editor() as editor:
                editor.create_model(Sector)
        # The Django model predates the shape (agri-db b8c2f0d5e713) and crop
        # (c9e1a2b3d4f5) columns, but the agri.db ORM maps them, as prod has
        # them: add them here so fastapp's sector reads see the live schema.
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    ALTER TABLE analytics_sector
                        ADD COLUMN IF NOT EXISTS geometry            JSONB,
                        ADD COLUMN IF NOT EXISTS area_ha             DOUBLE PRECISION,
                        ADD COLUMN IF NOT EXISTS perimeter_m         DOUBLE PRECISION,
                        ADD COLUMN IF NOT EXISTS color               VARCHAR(9),
                        ADD COLUMN IF NOT EXISTS geometry_updated_at TIMESTAMPTZ,
                        ADD COLUMN IF NOT EXISTS crop            VARCHAR(80),
                        ADD COLUMN IF NOT EXISTS variety         VARCHAR(80),
                        ADD COLUMN IF NOT EXISTS planting_date   DATE,
                        ADD COLUMN IF NOT EXISTS irrigation_type VARCHAR(30),
                        ADD COLUMN IF NOT EXISTS root_depth_cm   DOUBLE PRECISION,
                        ADD COLUMN IF NOT EXISTS is_active       BOOLEAN NOT NULL DEFAULT true
                    """
                )
        with connection.cursor() as cursor:
            cols = {
                c.name
                for c in connection.introspection.get_table_description(
                    cursor, Zone._meta.db_table
                )
            }
        if "sector_id" not in cols:
            with connection.schema_editor() as editor:
                editor.add_field(Zone, Zone._meta.get_field("sector"))
    yield


@pytest.fixture(scope="session", autouse=True)
def _create_farm_map_schema(django_db_setup, django_db_blocker, _create_sector_schema):
    """Farm-map tables (agri-db c9e1a2b3d4f5, agri-api#459):
    ``analytics_sensorinstallation`` +
    ``analytics_mappreference``. Not Django-modelled (fastapp reads them via
    Core tables), so create them with the migration's own DDL. Postgres only —
    the sqlite runs never reach the dual-ORM farm-map tests.

    No FKs here: Django's flush truncates only the tables it models, and a FK
    into ``CustomUser_customuser`` from an unmodelled table would make that
    TRUNCATE fail between transactional tests.
    """
    from django.db import connection

    if not connection.settings_dict["ENGINE"].endswith("postgresql"):
        yield
        return
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics_sensorinstallation (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL, sector_id BIGINT, zone_id BIGINT,
                device_id BIGINT, name VARCHAR(100) NOT NULL,
                sensor_type VARCHAR(40) NOT NULL, sensor_key VARCHAR(64),
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                installation_depth_cm DOUBLE PRECISION, model VARCHAR(100),
                serial_number VARCHAR(128), node_id VARCHAR(128),
                installation_date DATE, calibration_profile VARCHAR(100),
                notes TEXT, maintenance_status VARCHAR(20),
                client_ref VARCHAR(64),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS analytics_sensorinstallation_user_client_ref
                ON analytics_sensorinstallation (user_id, client_ref)
                WHERE client_ref IS NOT NULL
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics_mappreference (
                user_id BIGINT PRIMARY KEY,
                basemap VARCHAR(30) NOT NULL DEFAULT 'satellite_labels',
                layers JSONB NOT NULL DEFAULT '{}'::jsonb,
                camera JSONB,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    yield


@pytest.fixture(scope="session", autouse=True)
def _create_admin_tables(django_db_setup, django_db_blocker):
    """The business-admin tables (analytics_plan / _subscription / _invoice /
    _auditevent / _systemsetting) are unmanaged (self-deployed in prod via
    scripts/ensure_admin_tables.py). Create them once for the whole test DB so
    flush-based TransactionTestCases across all apps don't choke on a missing
    table.
    """
    from django.db import connection

    from apps.irrigation.models import (
        AuditEvent,
        Invoice,
        Plan,
        Subscription,
        SystemSetting,
    )

    with django_db_blocker.unblock():
        existing = connection.introspection.table_names()
        with connection.schema_editor() as editor:
            for model in (Plan, Subscription, Invoice, AuditEvent, SystemSetting):
                if model._meta.db_table not in existing:
                    editor.create_model(model)
    yield


@pytest.fixture(scope="session", autouse=True)
def _create_device_table(django_db_setup, django_db_blocker):
    """The device registry table (analytics_device) is unmanaged (self-deployed
    in prod via scripts/ensure_device_tables.py). Create it once for the whole
    test DB so flush-based TransactionTestCases don't choke on a missing table.
    The ``lora_uplink`` table (also unmanaged) is created here too so non-
    chirpstack tests — e.g. the device-health scan — can write uplinks.
    """
    from django.db import connection

    from apps.irrigation.models import Device
    from apps.lorawan.chirpstack.models import LoraUplink

    with django_db_blocker.unblock():
        existing = connection.introspection.table_names()
        with connection.schema_editor() as editor:
            for model in (Device, LoraUplink):
                if model._meta.db_table not in existing:
                    editor.create_model(model)
    yield


@pytest.fixture(scope="session", autouse=True)
def _create_notification_zone_tables(django_db_setup, django_db_blocker):
    """The custom notification-zone tables (analytics_notificationzone /
    analytics_notificationzonesensor) are unmanaged (self-deployed in prod via
    scripts/ensure_notification_zone_tables.py). Create them once for the whole
    test DB so flush-based TransactionTestCases don't choke on a missing table.
    """
    from django.db import connection

    from apps.alerts.models import NotificationZone, NotificationZoneSensor

    with django_db_blocker.unblock():
        existing = connection.introspection.table_names()
        with connection.schema_editor() as editor:
            for model in (NotificationZone, NotificationZoneSensor):
                if model._meta.db_table not in existing:
                    editor.create_model(model)
    yield


@pytest.fixture(scope="session", autouse=True)
def _create_irrigation_automation_tables(django_db_setup, django_db_blocker):
    """The irrigation-automation tables (analytics_irrigationprogram /
    analytics_outputcommand) are unmanaged (self-deployed in prod via
    scripts/ensure_irrigation_tables.py). Create them once for the test DB so
    flush-based TransactionTestCases don't choke on a missing table.
    """
    from django.db import connection

    from apps.irrigation.models import IrrigationProgram, OutputCommand

    with django_db_blocker.unblock():
        existing = connection.introspection.table_names()
        with connection.schema_editor() as editor:
            for model in (IrrigationProgram, OutputCommand):
                if model._meta.db_table not in existing:
                    editor.create_model(model)
    yield


@pytest.fixture(scope="session", autouse=True)
def _create_monitoring_tables(django_db_setup, django_db_blocker):
    """The monitoring tables (analytics_taskrun / _notificationdeliverylog /
    _loginevent) are unmanaged (self-deployed in prod via
    scripts/ensure_monitoring_tables.py). Create them once for the whole test DB
    so flush-based TransactionTestCases across all apps don't choke on a missing
    table.
    """
    from django.db import connection

    from apps.irrigation.models import (
        LoginEvent,
        NotificationDeliveryLog,
        TaskRun,
    )

    with django_db_blocker.unblock():
        existing = connection.introspection.table_names()
        with connection.schema_editor() as editor:
            for model in (TaskRun, NotificationDeliveryLog, LoginEvent):
                if model._meta.db_table not in existing:
                    editor.create_model(model)
    yield


@pytest.fixture(scope="session", autouse=True)
def _add_access_level_column(django_db_setup, django_db_blocker):
    """``CustomUser_customuser.access_level`` is added out-of-band in prod by
    the agri-db migration (0.19.0) — the Django CustomUser model does not
    declare it, so it is absent from Django's migration-built test DB. The
    fastapp RBAC dependency (#444) and the agri-db SQLAlchemy model both read
    this column, so create it once for the whole test DB (VARCHAR(16) NOT NULL
    DEFAULT 'editor', matching the agri-db column) so authenticated requests
    don't choke on a missing column.
    """
    from django.db import connection

    if not connection.settings_dict["ENGINE"].endswith("postgresql"):
        yield
        return

    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute(
            'ALTER TABLE "CustomUser_customuser" '
            "ADD COLUMN IF NOT EXISTS access_level VARCHAR(16) NOT NULL "
            "DEFAULT 'editor'"
        )
    yield


@pytest.fixture
def set_access_level():
    """Return ``set(user, level)`` — write a Django-created user's RBAC
    ``access_level`` (#444) with raw SQL (the Django CustomUser model has no
    such field). Under ``transaction=True`` tests the write commits, so the
    fastapp SQLAlchemy session (a separate connection) then reads the new tier.
    """
    from django.db import connection

    def _set(user, level: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE "CustomUser_customuser" SET access_level = %s WHERE id = %s',
                [level, user.pk],
            )

    return _set


def _bearer_client(user) -> APIClient:
    from rest_framework_simplejwt.tokens import AccessToken

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return client


@pytest.fixture
def admin_bearer(admin_user) -> APIClient:
    """APIClient carrying a Bearer token for a staff user."""
    return _bearer_client(admin_user)


@pytest.fixture
def user_bearer(normal_user) -> APIClient:
    """APIClient carrying a Bearer token for a normal (non-staff) user."""
    return _bearer_client(normal_user)


@pytest.fixture
def other_bearer(other_user) -> APIClient:
    """APIClient carrying a Bearer token for a second normal user."""
    return _bearer_client(other_user)
