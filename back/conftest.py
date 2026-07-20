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
