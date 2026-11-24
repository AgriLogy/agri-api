"""Regenerate ``scenarios_real.json``: pseudo-real data for the assistant eval (#461).

The eval answers tool calls from canned outputs. "Pseudo-real" outputs are what
the REAL assistant tool handlers return over a test farm's real readings, so the
model sees the payload shapes, labels, decimals and quirks it meets in
production. Two steps, both run inside an agri-api container (code in /code/src):

1. ``export`` (against the DB that holds the test farm, read-only): writes the
   48 h of readings ending at ``T_DRY`` for device ``SRC_DEVICE`` as JSON::

       python -m fastapp.assistant.eval.real_data export /tmp/window.json

2. ``build`` (against a LOCAL DB only — it creates and rewrites an
   ``eval-farm`` user): loads the window into that user's single zone for each
   scenario state, runs every read tool, prints the fixture::

       python -m fastapp.assistant.eval.real_data build /tmp/window.json \
           > src/fastapp/assistant/eval/scenarios_real.json

Current source: the Router01 test farm (imaginary), "Zone Sol", whose November
readings come from one device with weather from real Open-Meteo history.
``T_DRY`` is its driest moment (soil 29.95 %, just before an irrigation). States:

* dry    — readings shifted by whole days so the last one is 20 min before
           ``eval_now`` (the time of day stays true), crop configured;
* stale  — the same, 3 days older;
* empty  — no readings, no crop;
* alerts — dry, plus the zone's two real alerts; the soil one fired on the
           last reading.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from sqlalchemy import text

from agri.core.database import session_scope
from fastapp.assistant.tools import registry
from fastapp.auth import AuthedUser

SRC_DEVICE = 3  # Router01 / Zone Sol, November readings (test farm, imaginary)
T_DRY = dt.datetime(2026, 11, 2, 7, 0, tzinfo=dt.timezone.utc)
WINDOW = dt.timedelta(hours=48)
EVAL_USERNAME = "eval-farm"

# Prod's settings for Zone Sol (2026-11-24), except the critical threshold:
# 30 % is the zone's own earlier setting (prod had it until it was lowered to 25).
ZONE = {
    "name": "Zone Sol",
    "space": 144638,
    "critical_moisture_threshold": 30,
    "irrigation_water_quantity": 150000,
    "pomp_flow_rate": 6,
    "soil_param_FC": 30,
    "soil_param_RAW": 45,
    "soil_param_TAW": 90,
    "soil_param_WP": 15,
    "elevation_m": 790,
}
CROP = {"plant_name": "Pomme de terre", "name": "Pomme de terre d'arrière-saison"}
PERIODS = [
    ("Initial", "2026-10-10", "2026-11-03", 0.5),
    ("Développement", "2026-11-04", "2026-12-08", 0.85),
    ("Mi-saison", "2026-12-09", "2027-01-23", 1.15),
    ("Arrière-saison", "2027-01-24", "2027-02-27", 0.75),
]
TREND_KEYS = ["soilMoisture", "soilTemp", "airTemp", "humidity", "et0", "vpd"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def series_tables(s) -> list[str]:
    rows = s.execute(
        text(
            """
            select table_name from information_schema.columns
            where table_schema='public' and table_name like 'analytics_%'
              and column_name in ('user_id','timestamp','zone_id')
            group by table_name having count(*) = 3 order by 1
            """
        )
    ).scalars()
    return list(rows)


def columns(s, table: str) -> list[str]:
    return list(
        s.execute(
            text(
                "select column_name from information_schema.columns "
                "where table_schema='public' and table_name=:t order by ordinal_position"
            ),
            {"t": table},
        ).scalars()
    )


def export_window(out: Path) -> None:
    """Write ``{table: [row, ...]}`` for SRC_DEVICE's readings in the window."""
    window = {}
    with session_scope() as s:
        for t in series_tables(s):
            rows = s.execute(
                text(
                    f'select row_to_json(x)::text from "{t}" x where device_id=:d '
                    'and "timestamp" > :a and "timestamp" <= :b order by "timestamp"'
                ),
                {"d": SRC_DEVICE, "a": T_DRY - WINDOW, "b": T_DRY},
            ).scalars()
            rows = [json.loads(r) for r in rows]
            if rows:
                window[t] = rows
    out.write_text(json.dumps(window, default=str), encoding="utf-8")
    log(f"{sum(map(len, window.values()))} rows from {len(window)} tables -> {out}")


def ensure_eval_farm(s) -> tuple[int, int]:
    uid = s.execute(
        text('select id from "CustomUser_customuser" where username=:u'),
        {"u": EVAL_USERNAME},
    ).scalar()
    if uid is None:
        cols = [c for c in columns(s, "CustomUser_customuser") if c != "id"]
        sel = []
        for c in cols:
            if c == "username":
                sel.append(f"'{EVAL_USERNAME}'")
            elif c == "email":
                sel.append("'eval-farm@local.invalid'")
            elif c in ("is_staff", "is_superuser", "is_technician"):
                sel.append("false")
            elif c == "password":
                sel.append("'!'")  # unusable password: never logs in
            else:
                sel.append(f'"{c}"')
        col_sql = ", ".join(f'"{c}"' for c in cols)
        # Any existing row is the template for the columns not overridden.
        uid = s.execute(
            text(
                f'insert into "CustomUser_customuser" (id, {col_sql}) '
                f'select (select max(id)+1 from "CustomUser_customuser"), {", ".join(sel)} '
                'from "CustomUser_customuser" order by id limit 1 returning id'
            )
        ).scalar()
    zid = s.execute(
        text("select id from analytics_zone where user_id=:u order by id limit 1"),
        {"u": uid},
    ).scalar()
    if zid is None:
        cols = ", ".join(f'"{k}"' for k in ZONE)
        vals = ", ".join(f":{k}" for k in ZONE)
        zid = s.execute(
            text(
                f"insert into analytics_zone (id, user_id, {cols}) "
                f"values ((select max(id)+1 from analytics_zone), :u, {vals}) returning id"
            ),
            {"u": uid, **ZONE},
        ).scalar()
    sets = ", ".join(f'"{k}"=:{k}' for k in ZONE)
    s.execute(
        text(f"update analytics_zone set {sets} where id=:zid"), {**ZONE, "zid": zid}
    )
    return uid, zid


def set_crop(s, uid: int, zid: int, on: bool) -> None:
    kc_ids = list(
        s.execute(
            text("select id from analytics_kc where user_id=:u"), {"u": uid}
        ).scalars()
    )
    if kc_ids:
        pids = list(
            s.execute(
                text(
                    "select period_id from analytics_kcperiodassignment where kc_id = any(:k)"
                ),
                {"k": kc_ids},
            ).scalars()
        )
        s.execute(
            text("delete from analytics_kcperiodassignment where kc_id = any(:k)"),
            {"k": kc_ids},
        )
        if pids:
            s.execute(
                text("delete from analytics_kcperiod where id = any(:p)"), {"p": pids}
            )
        s.execute(text("delete from analytics_kc where id = any(:k)"), {"k": kc_ids})
    if not on:
        return
    kc = s.execute(
        text(
            "insert into analytics_kc (id, plant_name, name, number_of_periods, user_id, zone_id) "
            "values ((select coalesce(max(id),0)+1 from analytics_kc), :p, :n, :np, :u, :z) returning id"
        ),
        {
            "p": CROP["plant_name"],
            "n": CROP["name"],
            "np": len(PERIODS),
            "u": uid,
            "z": zid,
        },
    ).scalar()
    for name, start, end, value in PERIODS:
        pid = s.execute(
            text(
                "insert into analytics_kcperiod (id, period_name, start_date, end_date, kc_value) "
                "values ((select coalesce(max(id),0)+1 from analytics_kcperiod), :n, :a, :b, :v) returning id"
            ),
            {"n": name, "a": start, "b": end, "v": value},
        ).scalar()
        s.execute(
            text(
                "insert into analytics_kcperiodassignment (id, kc_id, period_id) "
                "values ((select coalesce(max(id),0)+1 from analytics_kcperiodassignment), :k, :p)"
            ),
            {"k": kc, "p": pid},
        )


def clear_state(s, uid: int, tables: list[str]) -> None:
    for t in tables:
        s.execute(text(f'delete from "{t}" where user_id=:u'), {"u": uid})
    s.execute(text("delete from analytics_alert where user_id=:u"), {"u": uid})


def load_readings(s, uid: int, zid: int, window: dict, shift: dt.timedelta) -> int:
    total = 0
    for t, rows in window.items():
        cols = [c for c in rows[0] if c != "id"]
        col_sql = ", ".join(f'"{c}"' for c in cols)
        val_sql = ", ".join(f":{c}" for c in cols)
        for row in rows:
            vals = {c: row[c] for c in cols}
            vals["user_id"], vals["zone_id"], vals["device_id"] = uid, zid, None
            vals["timestamp"] = dt.datetime.fromisoformat(row["timestamp"]) + shift
            s.execute(text(f'insert into "{t}" ({col_sql}) values ({val_sql})'), vals)
            total += 1
    return total


def add_alerts(s, uid: int, zid: int, triggered_at: dt.datetime) -> None:
    # The two alerts Router01 really has in prod; the soil one fired on the
    # latest (dry) reading.
    rows = [
        (
            "Sol sec — Zone Sol",
            "Humidité du sol (sonde moyenne) sous 30 %.",
            "Humidity",
            "<",
            30,
            "soil_moisture_medium",
            triggered_at,
        ),
        (
            "Sol froid",
            "Température du sol sous 5 °C : croissance ralentie.",
            "Soil Temperature",
            "<",
            5,
            "soil_temperature_medium",
            None,
        ),
    ]
    for name, desc, typ, cond, nbr, key, last in rows:
        s.execute(
            text(
                "insert into analytics_alert (id, description, name, type, condition, user_id, "
                "condition_nbr, created_at, is_active, last_triggered_at, sensor_key, updated_at, "
                "zone_id, notify_email, notify_whatsapp, notify_sms) values ("
                "(select coalesce(max(id),0)+1 from analytics_alert), :d, :n, :t, :c, :u, :nbr, now(), "
                "true, :last, :k, now(), :z, true, false, false)"
            ),
            {
                "d": desc,
                "n": name,
                "t": typ,
                "c": cond,
                "u": uid,
                "nbr": nbr,
                "last": last,
                "k": key,
                "z": zid,
            },
        )


def capture(uid: int, zid: int) -> dict:
    user = AuthedUser(
        id=uid,
        username=EVAL_USERNAME,
        email="eval-farm@local.invalid",
        is_staff=False,
        preferred_language="fr",
        access_level="editor",
    )
    out: dict = {}
    for tool in registry.all():
        if tool.mutating or tool.name in ("get_sensor_trend",):
            continue
        params = {
            "search_knowledge": {"query": "irrigation pomme de terre"},
            "get_zone_detail": {"zone_id": zid},
        }.get(tool.name, {})
        out[tool.name] = tool.handler(user, params)
    for key in TREND_KEYS:
        out[f"get_sensor_trend|{key}"] = registry.get("get_sensor_trend").handler(
            user, {"sensor_key": key, "hours": 24}
        )
    return out


def build(window_file: Path) -> None:
    window = json.loads(window_file.read_text(encoding="utf-8"))
    with session_scope(commit=True) as s:
        uid, zid = ensure_eval_farm(s)
        tables = series_tables(s)
    log(f"eval user {uid}, zone {zid}, {len(tables)} series tables")

    # Shift by whole days so the time of day stays true (a 07:00 reading still
    # reads as 07:00), and put the eval clock 20 min after the last reading.
    today_end = T_DRY + dt.timedelta(
        days=(dt.datetime.now(dt.timezone.utc).date() - T_DRY.date()).days
    )
    now = today_end + dt.timedelta(minutes=20)
    states = {
        "dry": {"latest": today_end, "crop": True, "alerts": False},
        "stale": {
            "latest": today_end - dt.timedelta(days=3),
            "crop": True,
            "alerts": False,
        },
        "empty": {"latest": None, "crop": False, "alerts": False},
        "alerts": {"latest": today_end, "crop": True, "alerts": True},
    }
    scenarios = {}
    for name, st in states.items():
        with session_scope(commit=True) as s:
            clear_state(s, uid, tables)
            set_crop(s, uid, zid, st["crop"])
            n = 0
            if st["latest"] is not None:
                n = load_readings(s, uid, zid, window, st["latest"] - T_DRY)
            if st["alerts"]:
                add_alerts(s, uid, zid, st["latest"])
        scenarios[name] = capture(uid, zid)
        log(f"{name}: {n} readings loaded, {len(scenarios[name])} tool outputs")

    with session_scope(commit=True) as s:  # leave the eval farm empty
        clear_state(s, uid, tables)

    meta = {
        "eval_now": now.isoformat(),
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "zone": {"id": zid, "name": ZONE["name"]},
        "source": (
            "Router01 test farm (imaginary), Zone Sol: its readings for the 48 h "
            f"ending {T_DRY.isoformat()} (driest point of November, soil 29.95 %, "
            "weather from real Open-Meteo history), shifted by whole days so the last "
            "reading is 20 min (stale: 3 days) before eval_now. Zone settings and crop as in prod "
            "on 2026-11-24, critical threshold 30 % (the zone's earlier setting). "
            "Tool outputs come from the real assistant tool handlers on the local stack."
        ),
    }
    print(
        json.dumps(
            {"meta": meta, "scenarios": scenarios},
            ensure_ascii=False,
            indent=1,
            default=str,
        )
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("export", "build"):
        log(__doc__)
        return 2
    (export_window if argv[0] == "export" else build)(Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
