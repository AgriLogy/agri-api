"""Idempotently create the technician RBAC schema.

Schema-of-record lives in agri-db (Alembic) and Django doesn't run `migrate` on
boot, so — like the assistant + LoraUplink tables — the technician RBAC schema
is created out-of-band here. Run on web boot (docker-entrypoint.sh): it creates
the two unmanaged grant tables and adds ``CustomUser.is_technician`` only if
they're missing, so it's safe to run every start.
"""

from __future__ import annotations

import os
import sys

import django

# src/ layout: make back/src/ importable when run outside the container.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agriapi.settings")
django.setup()

from django.db import connection  # noqa: E402

from apps.irrigation.models import (  # noqa: E402
    TechnicianGrant,
    TechnicianZoneGrant,
)
from apps.users.models import CustomUser  # noqa: E402

TAG = "[ensure-technician-tables]"


def _ensure_tables() -> None:
    existing = set(connection.introspection.table_names())
    with connection.schema_editor() as editor:
        for model in (TechnicianGrant, TechnicianZoneGrant):
            table = model._meta.db_table
            if table in existing:
                print(f"{TAG} {table} already exists — nothing to do.")
            else:
                editor.create_model(model)
                print(f"{TAG} created {table}.")


def _ensure_is_technician_column() -> None:
    table = CustomUser._meta.db_table
    with connection.cursor() as cursor:
        columns = {
            col.name
            for col in connection.introspection.get_table_description(cursor, table)
        }
    if "is_technician" in columns:
        print(f"{TAG} {table}.is_technician already exists — nothing to do.")
        return
    field = CustomUser._meta.get_field("is_technician")
    with connection.schema_editor() as editor:
        editor.add_field(CustomUser, field)
    print(f"{TAG} added {table}.is_technician.")


def ensure() -> None:
    _ensure_tables()
    _ensure_is_technician_column()


if __name__ == "__main__":
    try:
        ensure()
    except Exception as exc:  # never block boot on this
        print(f"{TAG} skipped/failed: {exc}", file=sys.stderr)
