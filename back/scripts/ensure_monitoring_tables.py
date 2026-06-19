"""Idempotently create the monitoring/observability tables.

Schema-of-record lives in agri-db (Alembic) and Django doesn't run `migrate` on
boot, so — like the assistant / technician / business-admin tables — these
unmanaged tables are created out-of-band here. Run on web boot
(docker-entrypoint.sh): it creates any of the three tables that are missing, so
it's safe to run every start.

  * analytics_taskrun                — one row per Celery task execution
  * analytics_notificationdeliverylog — one row per notification delivery attempt
  * analytics_loginevent             — one row per sign-in attempt
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
    LoginEvent,
    NotificationDeliveryLog,
    TaskRun,
)

TAG = "[ensure-monitoring-tables]"

MODELS = (TaskRun, NotificationDeliveryLog, LoginEvent)


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
