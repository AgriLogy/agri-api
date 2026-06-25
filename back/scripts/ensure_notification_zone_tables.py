"""Idempotently create the custom notification-zone tables.

Schema-of-record lives in agri-db (Alembic) and Django doesn't run `migrate` on
boot, so — like the device/technician/admin tables — ``analytics_notificationzone``
and ``analytics_notificationzonesensor`` are created out-of-band here. Run on web
boot (docker-entrypoint.sh): creates each table only if missing, safe every start.
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

from apps.alerts.models import NotificationZone, NotificationZoneSensor  # noqa: E402

TAG = "[ensure-notification-zone-tables]"


def ensure() -> None:
    existing = set(connection.introspection.table_names())
    with connection.schema_editor() as editor:
        for model in (NotificationZone, NotificationZoneSensor):
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
