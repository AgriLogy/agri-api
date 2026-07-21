"""Idempotently create analytics_sector + add analytics_zone.sector_id.

Schema-of-record lives in agri-db (Alembic, migration b33c23723140) and Django
doesn't run `migrate` on boot, so — like the device / notification-zone tables —
the sector table and the zone.sector_id column are created out-of-band here. Run
on web boot (docker-entrypoint.sh): creates only what's missing, safe every
start. FKs are omitted (db_constraint=False on the models), matching the other
ensure-created tables.
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

from apps.irrigation.models import Sector, Zone  # noqa: E402

TAG = "[ensure-sector-tables]"


def ensure() -> None:
    # 1. The sector table.
    existing = set(connection.introspection.table_names())
    if Sector._meta.db_table in existing:
        print(f"{TAG} {Sector._meta.db_table} already exists — nothing to do.")
    else:
        with connection.schema_editor() as editor:
            editor.create_model(Sector)
        print(f"{TAG} created {Sector._meta.db_table}.")

    # 2. The zone.sector_id column (add only if missing).
    with connection.cursor() as cursor:
        cols = {
            c.name
            for c in connection.introspection.get_table_description(
                cursor, Zone._meta.db_table
            )
        }
    if "sector_id" in cols:
        print(f"{TAG} {Zone._meta.db_table}.sector_id already exists — nothing to do.")
    else:
        with connection.schema_editor() as editor:
            editor.add_field(Zone, Zone._meta.get_field("sector"))
        print(f"{TAG} added {Zone._meta.db_table}.sector_id.")


if __name__ == "__main__":
    try:
        ensure()
    except Exception as exc:  # never block boot on this
        print(f"{TAG} skipped/failed: {exc}", file=sys.stderr)
