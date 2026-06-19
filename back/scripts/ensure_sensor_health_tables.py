"""Idempotently create the device-health sensor tables (battery / signal).

``BatterySensor`` and ``SignalSensor`` have a Django migration (0060) but the
schema-of-record is agri-db (Alembic) and Django doesn't run ``migrate`` on
boot, so on a fresh prod DB whose agri-db baseline predates these models the
tables are absent. Create them out-of-band here (web boot, docker-entrypoint.sh)
— safe to run every start; only missing tables are created.
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

from apps.sensors.models import BatterySensor, SignalSensor  # noqa: E402

TAG = "[ensure-sensor-health-tables]"

MODELS = (BatterySensor, SignalSensor)


def ensure() -> None:
    existing = set(connection.introspection.table_names())
    with connection.schema_editor() as editor:
        for model in MODELS:
            table = model._meta.db_table
            if table in existing:
                print(f"{TAG} {table} already exists — nothing to do.")
            else:
                editor.create_model(model)
                print(f"{TAG} created {table}.")


if __name__ == "__main__":
    try:
        ensure()
    except Exception as exc:  # never block boot on this
        print(f"{TAG} skipped/failed: {exc}", file=sys.stderr)
