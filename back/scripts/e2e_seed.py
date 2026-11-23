"""Seed run-unique tenants for the agri-web end-to-end suite (agrilogy-front#262).

    python scripts/e2e_seed.py            # create, print JSON on stdout
    python scripts/e2e_seed.py --cleanup  # delete every e2e-* tenant and its data
    python scripts/e2e_seed.py --demo     # + a realistic farm for the screenshot tour

Creates, for one test run (suffix = timestamp):

* farmer A — two zones, the first with a week of sensor history;
* farmer B — one zone (the "other tenant" for isolation checks);
* a technician with an active read grant on farmer A, scoped to zone A1.

``--demo`` (the screenshot tour, agri-web ``e2e/tour``) also gives farmer A a
farm worth looking at: two drawn sectors (one with both zones, soil water
parameters and a Kc table, one empty), metered irrigation, two months of
stored NDVI, a shape revision history, a water basin with a week of levels,
three alert rules with two weeks of firings and two weeks of daily
irrigation decisions (so Alerts and Reports are not empty).

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


def seed(demo: bool = False) -> dict:
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
    demo_data = None
    if demo:
        with transaction.atomic():
            demo_data = _seed_demo(a, zones_a)
        # After the commit: the events reference the rules through a separate
        # SQLAlchemy connection, which cannot see uncommitted Django rows.
        demo_data["alerts"], demo_data["decisions"] = _seed_demo_alerts_and_decisions(
            a, zones_a
        )

    return {
        "run": run,
        "readings_seeded": readings,
        "demo": demo_data,
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


# Open farmland in the Chaouia plain, north-east of Berrechid.
_DEMO_NORTH = [
    [-7.5222, 33.3014],
    [-7.5182, 33.3016],
    [-7.5179, 33.2986],
    [-7.5217, 33.2983],
    [-7.5222, 33.3014],
]
_DEMO_NORTH_OLD = [
    [-7.5222, 33.3014],
    [-7.5192, 33.3015],
    [-7.5190, 33.2986],
    [-7.5217, 33.2983],
    [-7.5222, 33.3014],
]
_DEMO_SOUTH = [
    [-7.5219, 33.2978],
    [-7.5177, 33.2980],
    [-7.5175, 33.2954],
    [-7.5215, 33.2952],
    [-7.5219, 33.2978],
]


def _seed_demo(a, zones_a) -> dict:
    """A realistic farm for farmer A (see module docstring)."""
    import datetime as dt
    import math

    from agri.core.geometry import area_hectares, perimeter_m
    from apps.irrigation.models import Kc, KcPeriod, KcPeriodAssignment
    from apps.sensors.models import WaterFlowSensor

    tables = _existing_tables()
    now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    today = now.date()

    def poly(ring):
        return {"type": "Polygon", "coordinates": [ring]}

    def sector(name, ring, crop, color):
        geom = poly(ring)
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO analytics_sector (name, user_id, geometry, area_ha, perimeter_m,"
                " color, geometry_updated_at, crop, irrigation_type, is_active)"
                " VALUES (%s, %s, %s::jsonb, %s, %s, %s, now(), %s, 'drip', true) RETURNING id",
                [
                    name,
                    a.id,
                    json.dumps(geom),
                    area_hectares(geom),
                    perimeter_m(geom),
                    color,
                    crop,
                ],
            )
            return cur.fetchone()[0], geom

    north_id, north_geom = sector("Parcelle Nord", _DEMO_NORTH, "Tomate", "#2E7D32")
    south_id, _ = sector("Parcelle Sud", _DEMO_SOUTH, "Pomme de terre", "#F9A825")

    for zone in zones_a:
        zone.sector_id = north_id
        zone.soil_param_TAW = 81.0
        zone.soil_param_RAW = 40.5
        zone.pomp_flow_rate = 4.0
        zone.irrigation_water_quantity = 150_000.0
        zone.humidity_weather_latitude = 33.3000
        zone.humidity_weather_longitude = -7.5200
        zone.save()

    kc = Kc.objects.create(
        name="Tomate plein champ",
        plant_name="Tomate",
        user=a,
        zone=zones_a[0],
        number_of_periods=3,
    )
    for name, start, end, value in (
        ("Initial", -70, -41, 0.6),
        ("Développement", -40, -11, 0.9),
        ("Mi-saison", -10, 50, 1.15),
    ):
        period = KcPeriod.objects.create(
            period_name=name,
            start_date=today + dt.timedelta(days=start),
            end_date=today + dt.timedelta(days=end),
            kc_value=value,
        )
        KcPeriodAssignment.objects.create(kc=kc, period=period)

    # Two irrigations of ~3 h at 14 m³/h, five and two days ago (06:00 UTC).
    flow = []
    for days_ago in (5, 2):
        start = (now - dt.timedelta(days=days_ago)).replace(hour=6)
        for minute in range(0, 180, 15):
            flow.append(
                WaterFlowSensor(
                    zone=zones_a[0],
                    user=a,
                    value=14.0,
                    timestamp=start + dt.timedelta(minutes=minute),
                )
            )
    WaterFlowSensor.objects.bulk_create(flow)

    if "sector_geometry_revision" in tables:
        with connection.cursor() as cur:
            for ring, source, age in (
                (_DEMO_NORTH_OLD, "kml", 30),
                (_DEMO_NORTH, "draw", 6),
            ):
                geom = poly(ring)
                cur.execute(
                    "INSERT INTO sector_geometry_revision (sector_id, user_id, geometry,"
                    " area_ha, perimeter_m, source, created_at)"
                    " VALUES (%s, %s, %s::jsonb, %s, %s, %s, now() - %s * interval '1 day')",
                    [
                        north_id,
                        a.id,
                        json.dumps(geom),
                        area_hectares(geom),
                        perimeter_m(geom),
                        source,
                        age,
                    ],
                )

    ndvi_points = 0
    if "sector_satellite_stat" in tables:
        from fastapp.satellite.store import geometry_hash

        ghash = geometry_hash(north_geom)
        with connection.cursor() as cur:
            for i, days_ago in enumerate(range(58, 0, -5)):
                # Green-up, then a slight dip over the last two weeks.
                mean = (
                    0.38
                    + 0.32 * math.sin(min(i, 8) / 8 * math.pi / 2)
                    - (0.015 * max(0, i - 8))
                )
                for kind, value in (
                    ("ndvi", mean),
                    ("ndmi", mean - 0.25),
                    ("ndre", mean - 0.2),
                ):
                    cur.execute(
                        "INSERT INTO sector_satellite_stat (sector_id, geometry_hash,"
                        " index_kind, acquired_on, mean, min, max, std, valid_fraction,"
                        " classes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
                        " ON CONFLICT DO NOTHING",
                        [
                            north_id,
                            ghash,
                            kind,
                            today - dt.timedelta(days=days_ago),
                            round(value, 3),
                            round(value - 0.2, 3),
                            round(value + 0.15, 3),
                            0.07,
                            0.96,
                            json.dumps({"low": 0.1, "medium": 0.5, "high": 0.4}),
                        ],
                    )
                ndvi_points += 1

    water_source_id = None
    if "water_source" in tables:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO water_source (user_id, name, source_type, geometry, zone_ids,"
                " max_height_m, length_m, width_m, minimum_reserve_m3, mode,"
                " created_at, updated_at)"
                " VALUES (%s, 'Bassin principal', 'BASIN', 'RECTANGULAR', %s::jsonb,"
                " 4.0, 12.0, 8.0, 20.0, 'SIMULATED', now(), now()) RETURNING id",
                [a.id, json.dumps([z.id for z in zones_a])],
            )
            water_source_id = cur.fetchone()[0]
            if "water_level_reading" in tables:
                for h in range(7 * 24, -1, -3):
                    level = 2.6 + 0.9 * math.cos(h / 24 * math.pi) * 0.5 - h * 0.002
                    cur.execute(
                        "INSERT INTO water_level_reading (water_source_id, device_id, level_m, mode,"
                        " quality, measured_at, received_at)"
                        " VALUES (%s, 'simulator', %s, 'SIMULATED', 'GOOD', now() - %s * interval '1 hour',"
                        " now() - %s * interval '1 hour')",
                        [water_source_id, round(level, 2), h, h],
                    )

    return {
        "sectors": [
            {"id": north_id, "name": "Parcelle Nord"},
            {"id": south_id, "name": "Parcelle Sud"},
        ],
        "kc_id": kc.id,
        "ndvi_points": ndvi_points,
        "water_source_id": water_source_id,
    }


def _seed_demo_alerts_and_decisions(a, zones_a) -> tuple[int, int]:
    """Alert rules + two weeks of firings and of daily irrigation decisions,
    written through the same history helpers the live alert and advice paths
    use, so the Alerts list and both Reports tabs have real-looking content."""
    import datetime as dt

    from agri.core.database import session_scope
    from apps.alerts.models import Alert
    from fastapp.history import (
        build_alert_event_row,
        build_decision_row_from_advice,
        record_irrigation_decision,
    )
    from fastapp.history import _INSERT_ALERT_EVENT  # noqa: PLC2701 — seed only

    now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    z1 = zones_a[0]
    rules = [
        Alert.objects.create(
            user=a,
            zone=z1,
            name="Sol sec — Zone A1",
            type="Humidity",
            description="Humidité du sol (sonde moyenne) sous 28 %.",
            condition="<",
            condition_nbr=28,
            sensor_key="soil_moisture_medium",
            is_active=True,
            notify_email=True,
        ),
        Alert.objects.create(
            user=a,
            zone=z1,
            name="Risque de gel",
            type="Weather Temperature",
            description="Température de l'air sous 3 °C.",
            condition="<",
            condition_nbr=3,
            sensor_key="temperature_weather",
            is_active=True,
            notify_email=True,
            notify_sms=True,
        ),
        Alert.objects.create(
            user=a,
            zone=z1,
            name="Vent fort",
            type="Wind Speed",
            description="Vent au-dessus de 10 m/s : suspendre l'aspersion.",
            condition=">",
            condition_nbr=10,
            sensor_key="wind_speed",
            is_active=True,
            notify_email=True,
        ),
    ]
    firings = [  # (rule index, days ago, hour UTC, observed, unit)
        (0, 13, 14, 26.8, "%"),
        (0, 10, 15, 25.9, "%"),
        (0, 6, 13, 27.2, "%"),
        (2, 9, 12, 11.4, "m/s"),
        (2, 4, 16, 12.1, "m/s"),
        (1, 3, 5, 2.4, "°C"),
    ]
    decisions = 0
    with session_scope(commit=True) as session:
        for idx, days_ago, hour, observed, unit in firings:
            at = (now - dt.timedelta(days=days_ago)).replace(hour=hour, minute=10)
            row = build_alert_event_row(
                alert=rules[idx],
                user_id=a.id,
                sensor_key=rules[idx].sensor_key,
                observed_value=observed,
                reading_at=at,
                unit=unit,
                zone_id=z1.id,
                channels=["email"],
                triggered_at=at,
            )
            session.execute(_INSERT_ALERT_EVENT, row)
        for days_ago in range(14, 0, -1):
            at = (now - dt.timedelta(days=days_ago)).replace(hour=6, minute=0)
            dr = 8.0 + (14 - days_ago) % 4 * 9.5  # refills every 4 days
            irrigate = dr >= 36.5
            advice = {
                "recommendation": "irrigate" if irrigate else "no_irrigation",
                "estimated_water_m3": 21.6 if irrigate else 0.0,
                "estimated_duration_min": 90 if irrigate else 0,
                "reason": (
                    f"Irriguer 21,6 m³ (≈ 90 min) — déficit {dr:.1f} mm proche de la RFU."
                    if irrigate
                    else f"Pas d'irrigation nécessaire — déficit {dr:.1f} mm sous la RFU."
                ).replace(".", ",", 1),
                "dr_today_mm": dr,
                "raw_mm": 40.5,
                "et0_mm": 2.6,
                "soil_moisture_pct": 36.0 - dr / 3,
                "critical_moisture_threshold": 25,
                "zone_area_m2": 12000,
                "decision_source": "daily_advice",
                "zone_name": z1.name,
            }
            record_irrigation_decision(
                session,
                build_decision_row_from_advice(
                    user_id=a.id,
                    zone_id=z1.id,
                    source="daily_advice",
                    advice=advice,
                    decision_reason="stress" if irrigate else "no_stress",
                    decided_at=at,
                ),
            )
            decisions += 1
    return len(rules), decisions


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
        data = seed(demo="--demo" in argv)
    print(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
