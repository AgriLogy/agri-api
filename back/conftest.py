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
