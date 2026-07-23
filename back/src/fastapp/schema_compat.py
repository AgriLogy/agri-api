"""COMPATIBILITY SHIMS — schema features the live database may not have yet.

Two probes live here, both built the same way: a single dialect-neutral
``Inspector`` call per engine per process, cached, never re-run per request.

* :func:`device_coordinates_available` — the ``analytics_device`` coordinate
  COLUMNS (#436, migration ``b2c3d4e5f6a7``);
* :func:`table_available` (+ the ``sensor_groups_available`` /
  ``sensor_calibration_available`` wrappers) — whole TABLES that a held
  migration would create (#439, migration ``f4b6d2e8c1a9``).

--- #436: ``analytics_device.latitude`` / ``longitude`` ---------------------

``analytics_device`` gained ``latitude``/``longitude`` (the device-map feature)
in the agri-db migration ``b2c3d4e5f6a7``. That migration is deliberately HELD
and is NOT applied to production, so on the live schema those columns simply do
not exist and any statement naming them dies with ``psycopg.errors
.UndefinedColumn``.

The sibling fixes (#433 ``tasks_scan``, #435 ``ingest``) just stopped selecting
the whole entity — their code never needed the coordinates. The device-map
endpoints DO need them, so they instead ask this module whether the columns are
there and degrade:

* reads  → the coordinates come back ``null`` (no pin on the map: the honest
  state today) instead of the request 500-ing;
* writes → a payload that actually carries coordinates is rejected with
  :data:`DEVICE_COORDINATES_UNAVAILABLE` (see ``routers/devices.py``).

Detection is a single dialect-neutral column probe per engine per process
(``Inspector.get_columns``), cached — never an ``information_schema`` query per
request.

THIS IS NOT THE FIX. The fix is applying the migration; once it is applied the
probe returns ``True`` forever and every call site behaves exactly as it did
before the shim. Deleting it is then mechanical: remove this module, drop the
``device_coordinates_available`` calls in ``routers/selfreads.py`` and
``routers/devices.py`` (the only two importers), and inline the coordinate
columns back into their SQL.
"""

from __future__ import annotations

import logging
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import inspect as sa_inspect

log = logging.getLogger("fastapp.schema_compat")

DEVICE_TABLE = "analytics_device"
DEVICE_COORDINATE_COLUMNS = ("latitude", "longitude")

#: Error message for a write that carries coordinates the schema cannot store.
#: Names the migration so the operator knows exactly what to apply.
DEVICE_COORDINATES_UNAVAILABLE = (
    "Device coordinates are not available on this deployment: "
    "analytics_device has no latitude/longitude column. Apply the agri-db "
    "migration that adds them (b2c3d4e5f6a7), then retry. Every other device "
    "field can be saved in the meantime."
)

# One probe per Engine per process. Weak keys so a disposed engine (tests
# create one per case) does not pin anything alive.
_probe_cache: WeakKeyDictionary[Any, bool] = WeakKeyDictionary()


def _engine_of(session: Any) -> Any:
    """The Engine behind a Session — ``get_bind()`` may hand back a Connection."""
    bind = session.get_bind()
    return getattr(bind, "engine", bind)


def _probe(engine: Any) -> bool:
    """Are BOTH coordinate columns present on ``analytics_device``?

    Dialect-neutral (works on Postgres and on the sqlite mirrors the tests
    build) and failure-tolerant: an unreadable/absent table is reported as "no
    coordinates", which is the safe degradation — never a 500.
    """
    try:
        names = {c["name"] for c in sa_inspect(engine).get_columns(DEVICE_TABLE)}
    except Exception:  # pragma: no cover - table absent / inspection refused
        log.warning(
            "could not inspect %s; assuming no coordinate columns", DEVICE_TABLE
        )
        return False
    return all(column in names for column in DEVICE_COORDINATE_COLUMNS)


def device_coordinates_available(session: Any) -> bool:
    """``True`` when ``analytics_device`` has ``latitude`` AND ``longitude``.

    Evaluated once per engine per process and cached, so the hot request path
    pays nothing after the first call.
    """
    engine = _engine_of(session)
    cached = _probe_cache.get(engine)
    if cached is None:
        cached = _probe(engine)
        _probe_cache[engine] = cached
    return cached


def reset_device_coordinates_cache() -> None:
    """Forget every probed engine. Tests only — production probes once."""
    _probe_cache.clear()


# ---------------------------------------------------------------------------
# #439 — whole tables added by the sensor-groups / calibration migration
# ---------------------------------------------------------------------------
# ``analytics_sensorgroup`` / ``analytics_sensorgroupsensor`` /
# ``analytics_sensorcalibration`` are created by the agri-db migration
# ``f4b6d2e8c1a9``, which is NOT applied to production. Naming an absent table
# raises ``psycopg.errors.UndefinedTable`` and 500s the whole request, so the
# routers ask here first and degrade: reads answer empty / defaults, writes are
# refused with a message naming the migration.
#
# Same mechanism as the column probe above — one ``Inspector.has_table`` per
# (engine, table) per process, cached — deliberately NOT a second mechanism.
SENSOR_GROUP_TABLE = "analytics_sensorgroup"
SENSOR_GROUP_SENSOR_TABLE = "analytics_sensorgroupsensor"
SENSOR_CALIBRATION_TABLE = "analytics_sensorcalibration"

#: The agri-db revision that creates all three tables.
SENSOR_GROUPS_MIGRATION = "f4b6d2e8c1a9"

SENSOR_GROUPS_UNAVAILABLE = (
    "Sensor groups are not available on this deployment: "
    f"{SENSOR_GROUP_TABLE} does not exist. Apply the agri-db migration that "
    f"creates it ({SENSOR_GROUPS_MIGRATION}), then retry."
)

SENSOR_CALIBRATION_UNAVAILABLE = (
    "Sensor calibration is not available on this deployment: "
    f"{SENSOR_CALIBRATION_TABLE} does not exist. Apply the agri-db migration "
    f"that creates it ({SENSOR_GROUPS_MIGRATION}), then retry."
)

# {engine: {table_name: present}} — weak keys, exactly like _probe_cache.
_table_cache: WeakKeyDictionary[Any, dict[str, bool]] = WeakKeyDictionary()


def _probe_table(engine: Any, table: str) -> bool:
    """Does ``table`` exist on this engine?

    Dialect-neutral (``Inspector.has_table`` works on Postgres and on the
    sqlite mirrors the tests build) and failure-tolerant: an inspection that
    cannot be performed is reported as "absent", the safe degradation.
    """
    try:
        return bool(sa_inspect(engine).has_table(table))
    except Exception:  # pragma: no cover - inspection refused
        log.warning("could not inspect %s; assuming it is absent", table)
        return False


def table_available(session: Any, table: str) -> bool:
    """``True`` when ``table`` exists in the bound schema.

    Probed once per (engine, table) per process and cached, so the hot request
    path pays nothing after the first call.
    """
    engine = _engine_of(session)
    probed = _table_cache.get(engine)
    if probed is None:
        probed = {}
        _table_cache[engine] = probed
    cached = probed.get(table)
    if cached is None:
        cached = _probe_table(engine, table)
        probed[table] = cached
    return cached


def sensor_groups_available(session: Any) -> bool:
    """``True`` when BOTH sensor-group tables exist (a group is useless without
    its membership table, so the pair is probed as one feature)."""
    return table_available(session, SENSOR_GROUP_TABLE) and table_available(
        session, SENSOR_GROUP_SENSOR_TABLE
    )


def sensor_calibration_available(session: Any) -> bool:
    """``True`` when ``analytics_sensorcalibration`` exists."""
    return table_available(session, SENSOR_CALIBRATION_TABLE)


def reset_table_cache() -> None:
    """Forget every probed table. Tests only — production probes once."""
    _table_cache.clear()


__all__ = [
    "DEVICE_COORDINATES_UNAVAILABLE",
    "DEVICE_COORDINATE_COLUMNS",
    "DEVICE_TABLE",
    "SENSOR_CALIBRATION_TABLE",
    "SENSOR_CALIBRATION_UNAVAILABLE",
    "SENSOR_GROUPS_MIGRATION",
    "SENSOR_GROUPS_UNAVAILABLE",
    "SENSOR_GROUP_SENSOR_TABLE",
    "SENSOR_GROUP_TABLE",
    "device_coordinates_available",
    "reset_device_coordinates_cache",
    "reset_table_cache",
    "sensor_calibration_available",
    "sensor_groups_available",
    "table_available",
]
