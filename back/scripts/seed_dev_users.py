"""
Idempotent dev-user seed.

Runs once on every container boot (called from docker-entrypoint.sh,
web role). Recreates the legacy `user1` test account so the frontend
has someone to log in as right after a fresh `docker compose up`.

This is a *dev convenience*, gated by SEED_DEV_USERS=true (default in
back/.env for local stacks). Set it to false in production env files.
"""

from __future__ import annotations

import os
import sys

import django

# src/ layout: make back/src/ importable when run outside the container
# (the Docker image sets PYTHONPATH=/code/src; this covers local runs).
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agriapi.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402

from analytics.models import (  # noqa: E402
    ActiveGraph,
    GraphName,
    SensorColor,
    Zone,
)

User = get_user_model()

# (username, password, email, firstname) — kept deliberately small so
# adding another fixture is one tuple.
FIXTURES = [
    ("user1", "@Agrogo321", "john.doe@example.com", "John"),
    ("contact-agrilogy", "pw-dev", "contact.agrilogy@gmail.com", "Agrilogy"),
]


def seed() -> int:
    if os.getenv("SEED_DEV_USERS", "true").lower() not in ("1", "true", "yes"):
        print("[seed] SEED_DEV_USERS disabled — skipping.")
        return 0

    created = 0
    for username, password, email, firstname in FIXTURES:
        user, was_new = User.objects.get_or_create(
            username=username,
            defaults={
                "email": email,
                "firstname": firstname,
                "lastname": "Doe",
                "is_active": True,
            },
        )
        # Always reset password / contact info so a forgotten password
        # doesn't keep someone locked out of their own dev box.
        user.email = email
        user.firstname = firstname
        user.is_active = True
        user.set_password(password)
        user.save()

        zone, _ = Zone.objects.get_or_create(
            user=user,
            name="zone de marichage 1",
            defaults={
                "space": 1750.0,
                "critical_moisture_threshold": 18.0,
                "pomp_flow_rate": 1.0,
            },
        )
        # Backfill the dashboard wiring for zones that pre-existed the
        # post_save signal. Defaults on ActiveGraph turn every chart ON.
        ActiveGraph.objects.get_or_create(user=user, zone=zone)
        GraphName.objects.get_or_create(user=user, zone=zone)
        SensorColor.objects.get_or_create(user=user, zone=zone)
        if was_new:
            created += 1
            print(f"[seed] created user '{username}'")
        else:
            print(f"[seed] refreshed user '{username}'")
    print(f"[seed] done — {created} new, {len(FIXTURES) - created} refreshed.")
    return 0


if __name__ == "__main__":
    sys.exit(seed())
