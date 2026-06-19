"""Idempotently create the device registry table.

Schema-of-record lives in agri-db (Alembic) and Django doesn't run `migrate` on
boot, so — like the assistant, technician and admin tables — the ``Device``
table is created out-of-band here. Run on web boot (docker-entrypoint.sh): it
creates ``analytics_device`` only if missing, so it's safe every start.
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

from apps.irrigation.models import Device  # noqa: E402

TAG = "[ensure-device-tables]"


def ensure() -> None:
    existing = set(connection.introspection.table_names())
    with connection.schema_editor() as editor:
        table = Device._meta.db_table
        if table in existing:
            print(f"{TAG} {table} already exists — nothing to do.")
        else:
            editor.create_model(Device)
            print(f"{TAG} created {table}.")


if __name__ == "__main__":
    try:
        ensure()
    except Exception as exc:  # never block boot on this
        print(f"{TAG} skipped/failed: {exc}", file=sys.stderr)
