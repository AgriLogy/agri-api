"""Idempotently create the irrigation-automation tables.

Schema-of-record lives in agri-db (Alembic) and Django doesn't run `migrate` on
boot, so — like the assistant, technician, admin and device tables — the
``IrrigationProgram`` and ``OutputCommand`` tables are created out-of-band here.
Run on web boot (docker-entrypoint.sh): each is created only if missing, so it's
safe every start.
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

from apps.irrigation.models import IrrigationProgram, OutputCommand  # noqa: E402

TAG = "[ensure-irrigation-tables]"


def ensure() -> None:
    existing = set(connection.introspection.table_names())
    for model in (IrrigationProgram, OutputCommand):
        table = model._meta.db_table
        if table in existing:
            print(f"{TAG} {table} already exists — nothing to do.")
            continue
        with connection.schema_editor() as editor:
            editor.create_model(model)
        print(f"{TAG} created {table}.")


if __name__ == "__main__":
    try:
        ensure()
    except Exception as exc:  # never block boot on this
        print(f"{TAG} skipped/failed: {exc}", file=sys.stderr)
