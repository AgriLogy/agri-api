"""Seed run-unique tenants for the agri-web end-to-end suite (agrilogy-front#262).

    python scripts/e2e_seed.py            # create, print JSON on stdout
    python scripts/e2e_seed.py --cleanup  # delete every e2e-* tenant and its data

Creates, for one test run (suffix = timestamp):

* farmer A — two zones, the first with a week of sensor history;
* farmer B — one zone (the "other tenant" for isolation checks);
* a technician with an active read grant on farmer A, scoped to zone A1.

Each user gets JWT access/refresh tokens minted here, so the browser suite
never needs a password. Usernames all start with ``e2e-`` and ``--cleanup``
removes exactly those users, their zones and readings (Django cascades) plus
the rows of tables that carry a bare ``user_id`` without an FK.

LOCAL / TEST DATABASES ONLY: refuses to run unless DEBUG is on.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agriapi.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from rest_framework_simplejwt.tokens import RefreshToken  # noqa: E402

PREFIX = "e2e-"
# Tables keyed by a bare user_id / water_source_id (no FK, so no cascade).
_LOOSE_USER_TABLES = ("water_source", "notification_push_token")
_WATER_CHILD_TABLES = ("water_level_device", "water_level_reading", "water_level_alert")


def _tokens(user) -> dict[str, str]:
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def _existing_tables() -> set[str]:
    return set(connection.introspection.table_names())


def cleanup() -> dict[str, int]:
    User = get_user_model()
    users = list(User.objects.filter(username__startswith=PREFIX))
    ids = [u.id for u in users]
    removed = {"users": len(ids)}
    if not ids:
        return removed
    tables = _existing_tables()
    with transaction.atomic(), connection.cursor() as cursor:
        if "water_source" in tables:
            cursor.execute("SELECT id FROM water_source WHERE user_id = ANY(%s)", [ids])
            source_ids = [r[0] for r in cursor.fetchall()]
            for table in _WATER_CHILD_TABLES:
                if table in tables and source_ids:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE water_source_id = ANY(%s)",  # noqa: S608
                        [source_ids],
                    )
        for table in _LOOSE_USER_TABLES:
            if table in tables:
                cursor.execute(f"DELETE FROM {table} WHERE user_id = ANY(%s)", [ids])  # noqa: S608
        if {"analytics_technicianzonegrant", "analytics_techniciangrant"} <= tables:
            cursor.execute(
                "DELETE FROM analytics_technicianzonegrant WHERE grant_id IN ("
                "SELECT id FROM analytics_techniciangrant "
                "WHERE technician_id = ANY(%s) OR owner_id = ANY(%s))",
                [ids, ids],
            )
        if "analytics_techniciangrant" in tables:
            cursor.execute(
                "DELETE FROM analytics_techniciangrant "
                "WHERE technician_id = ANY(%s) OR owner_id = ANY(%s)",
                [ids, ids],
            )
        for user in users:
            user.delete()
    return removed


def seed() -> dict:
    from analytics.models import TechnicianGrant, TechnicianZoneGrant, Zone

    seed_dev = _load_seed_dev_data()
    User = get_user_model()
    run = time.strftime("%m%d%H%M%S")

    def user(role: str, **extra):
        name = f"{PREFIX}{run}-{role}"
        u = User.objects.create_user(
            username=name,
            email=f"{name}@example.test",
            password=None,  # unusable: the suite authenticates with tokens
            **extra,
        )
        return u

    with transaction.atomic():
        a = (
            user("a", firstname="Farmer A")
            if _has_field(User, "firstname")
            else user("a")
        )
        b = user("b")
        tech = user("tech", is_technician=True)
        zones_a = [
            Zone.objects.create(
                user=a, name="E2E Zone A1", space=12000, critical_moisture_threshold=25
            ),
            Zone.objects.create(
                user=a, name="E2E Zone A2", space=8000, critical_moisture_threshold=25
            ),
        ]
        zone_b = Zone.objects.create(
            user=b, name="E2E Zone B1", space=5000, critical_moisture_threshold=25
        )
        grant = TechnicianGrant.objects.create(technician=tech, owner=a, is_active=True)
        # Zone-scoped: the technician sees A1 only (A2 stays hidden).
        TechnicianZoneGrant.objects.create(
            grant=grant, zone=zones_a[0], allowed_graphs=["soil_moisture_low"]
        )

    readings = seed_dev._seed_zone(zones_a[0]) if seed_dev else 0

    return {
        "run": run,
        "readings_seeded": readings,
        "users": {
            "a": {
                "id": a.id,
                "username": a.username,
                "zones": [{"id": z.id, "name": z.name} for z in zones_a],
                **_tokens(a),
            },
            "b": {
                "id": b.id,
                "username": b.username,
                "zones": [{"id": zone_b.id, "name": zone_b.name}],
                **_tokens(b),
            },
            "tech": {
                "id": tech.id,
                "username": tech.username,
                "owner_id": a.id,
                "zones": [{"id": zones_a[0].id, "name": zones_a[0].name}],
                **_tokens(tech),
            },
        },
    }


def _has_field(model, name: str) -> bool:
    return any(f.name == name for f in model._meta.get_fields())


def _load_seed_dev_data():
    """Reuse the dev seeder's week of season-aware readings, quietly."""
    import contextlib
    import importlib.util
    import io

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_dev_data.py")
    spec = importlib.util.spec_from_file_location("seed_dev_data", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    if not settings.DEBUG:
        print(
            "e2e_seed refuses to run with DEBUG off (local/test databases only).",
            file=sys.stderr,
        )
        return 2
    if "--cleanup" in argv:
        print(json.dumps(cleanup()))
        return 0
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        data = seed()
    print(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
