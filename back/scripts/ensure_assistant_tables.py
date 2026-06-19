"""Idempotently create the assistant's tables.

Schema-of-record lives in agri-db (Alembic) and Django doesn't run `migrate` on
boot, so — like the LoraUplink table — the assistant's history table is created
out-of-band here. Run on web boot (docker-entrypoint.sh): it creates the table
only if it's missing, so it's safe to run every start.
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

from apps.assistant.models import AssistantConversation, ProactiveNotice  # noqa: E402


def ensure() -> None:
    existing = set(connection.introspection.table_names())
    for model in (AssistantConversation, ProactiveNotice):
        table = model._meta.db_table
        if table in existing:
            print(f"[ensure-assistant-tables] {table} already exists — nothing to do.")
            continue
        with connection.schema_editor() as editor:
            editor.create_model(model)
        print(f"[ensure-assistant-tables] created {table}.")


if __name__ == "__main__":
    try:
        ensure()
    except Exception as exc:  # never block boot on this
        print(f"[ensure-assistant-tables] skipped/failed: {exc}", file=sys.stderr)
