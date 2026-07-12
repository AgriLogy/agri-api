"""fastapp device tasks — background data maintenance for the device registry.

``backfill_device_readings`` migrates a device's historical sensor readings from
its *previous* account/zone to the one it was just attributed to. Enqueued by
``POST /devices/bulk-assign`` (``backfill=True``).

Why this exists: captors are commissioned under a **technician** account (or sit
in the shared ``lora`` catch-all while unassigned), then transferred to the
**client**. The client must see the device's full history from day one — so a
transfer moves every past reading, not just new ones.

Readings carry no DevEUI (only ``user_id``/``zone_id``/``value``/``timestamp``),
so we can't tag an individual reading to a device directly. Two strategies:

* **Full move** (default, complete): when the source zone holds only this one
  device, ALL of the source zone's readings are this device's → move them all.
  This is the common case (a captor commissioned in its own test zone, or the
  only device in ``lora``) and it transfers the *entire* history.
* **Correlated move** (fallback, best-effort): when the source zone is shared by
  several devices, only readings whose ``timestamp`` matches this device's raw
  ``lora_uplink.received_at`` (±2s) are moved, to avoid stealing another
  device's data. This can miss readings with no uplink row.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from agri.core.database import session_scope

log = logging.getLogger("fastapp.tasks_devices")

# Sensor tables a ChirpStack LoRa uplink writes — see fastapp.ingest
# ``handle_chirpstack_uplink`` (pH / battery / signal). Hardcoded (never
# interpolated from input) so the f-strings below carry no injection risk.
_LORA_SENSOR_TABLES = (
    "analytics_phsoil",
    "analytics_batterysensor",
    "analytics_signalsensor",
)
# A reading's timestamp and its raw uplink's received_at are both computed at
# ~now() a few ms apart during ingest; ±2s absorbs that jitter with margin.
_CORRELATION_WINDOW_SECONDS = 2


def backfill_device_readings(
    device_id: int,
    target_user_id: int,
    target_zone_id: int,
    source_user_id: int | None = None,
    source_zone_id: int | None = None,
) -> dict:
    """Move a device's past readings from its previous account/zone to the new
    one. ``source_*`` is the device's attribution BEFORE the transfer (captured
    by the bulk-assign endpoint); when ``source_zone_id`` is None the device was
    unassigned, so its readings live in the shared ``lora`` catch-all zone.

    Idempotent — once moved, rows no longer match the source filter. Returns the
    resolved mode (``full`` / ``correlated``) and per-table moved-row count.
    """
    with session_scope(commit=True) as session:
        dev = session.execute(
            text("SELECT serial FROM analytics_device WHERE id = :id"),
            {"id": device_id},
        ).first()
        if dev is None:
            log.warning("backfill: device %s not found", device_id)
            return {"device_id": device_id, "moved": {}, "skipped": "device_not_found"}
        dev_eui = dev.serial

        lora = session.execute(
            text(
                "SELECT id, user_id FROM analytics_zone "
                "WHERE name = 'lora' ORDER BY id LIMIT 1"
            )
        ).first()

        # Resolve the source (where the device's history currently lives).
        if source_zone_id is None:
            if lora is None:
                return {"device_id": device_id, "moved": {}, "skipped": "no_lora_zone"}
            source_zone_id, source_user_id = lora.id, lora.user_id
        elif source_user_id is None:
            z = session.execute(
                text("SELECT user_id FROM analytics_zone WHERE id = :z"),
                {"z": source_zone_id},
            ).first()
            source_user_id = z.user_id if z else None

        # No-op transfer to the same place.
        if source_zone_id == target_zone_id and source_user_id == target_user_id:
            return {"device_id": device_id, "moved": {}, "skipped": "same_zone"}

        # Is the source a single-device source (safe to move everything)? Devices
        # routing to ``lora`` are the unassigned ones (zone_id IS NULL); a normal
        # zone's devices point at it directly. Exclude the device being moved.
        is_lora_source = lora is not None and source_zone_id == lora.id
        if is_lora_source:
            others = session.execute(
                text(
                    "SELECT count(*) FROM analytics_device "
                    "WHERE is_active AND zone_id IS NULL AND id <> :id"
                ),
                {"id": device_id},
            ).scalar_one()
        else:
            others = session.execute(
                text(
                    "SELECT count(*) FROM analytics_device "
                    "WHERE is_active AND zone_id = :z AND id <> :id"
                ),
                {"z": source_zone_id, "id": device_id},
            ).scalar_one()
        full = others == 0

        params = {
            "dev_eui": dev_eui,
            "src_zone": source_zone_id,
            "src_user": source_user_id,
            "tu": target_user_id,
            "tz": target_zone_id,
            "win": f"{_CORRELATION_WINDOW_SECONDS} seconds",
        }
        base = "r.zone_id = :src_zone AND r.user_id = :src_user"
        correlate = (
            " AND EXISTS (SELECT 1 FROM lora_uplink lu WHERE lu.dev_eui = :dev_eui "
            "AND lu.received_at BETWEEN r.timestamp - CAST(:win AS interval) "
            "AND r.timestamp + CAST(:win AS interval))"
        )
        predicate = base if full else base + correlate

        moved: dict[str, int] = {}
        for table in _LORA_SENSOR_TABLES:
            n = session.execute(
                text(f"SELECT count(*) FROM {table} r WHERE {predicate}"), params
            ).scalar_one()
            log.info(
                "backfill dev_eui=%s table=%s rows=%s mode=%s -> user=%s zone=%s",
                dev_eui,
                table,
                n,
                "full" if full else "correlated",
                target_user_id,
                target_zone_id,
            )
            if n:
                session.execute(
                    text(
                        f"UPDATE {table} r SET user_id = :tu, zone_id = :tz "
                        f"WHERE {predicate}"
                    ),
                    params,
                )
            moved[table] = int(n)
        return {
            "device_id": device_id,
            "dev_eui": dev_eui,
            "mode": "full" if full else "correlated",
            "moved": moved,
        }
