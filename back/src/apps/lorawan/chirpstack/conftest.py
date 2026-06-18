"""Test fixtures for the ChirpStack LoRaWAN app.

``lora_uplink`` is intentionally **not** a migrated table — see the
``LoraUplink`` model docstring: in prod the table is created out-of-band via
``schema_editor().create_model(LoraUplink)``, not by a Django migration. Since
pytest-django builds the test database purely from migrations, the table is
absent there and the persistence tests fail with ``relation "lora_uplink" does
not exist``.

This session-scoped fixture creates the table the same way prod does, once,
mirroring the deploy-time step. It runs outside the per-test transaction so the
table persists for the whole session while each test's *data* still rolls back.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _create_lora_uplink_table(django_db_setup, django_db_blocker):
    from django.db import connection

    from apps.lorawan.chirpstack.models import LoraUplink

    table = LoraUplink._meta.db_table
    with django_db_blocker.unblock():
        if table not in connection.introspection.table_names():
            with connection.schema_editor() as editor:
                editor.create_model(LoraUplink)
    yield
