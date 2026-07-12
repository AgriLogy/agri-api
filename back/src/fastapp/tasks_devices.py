"""fastapp device tasks — background data maintenance for the device registry.

``backfill_device_readings`` migrates a device's historical sensor readings out
of the shared ``lora`` catch-all zone into the account/zone it was just
attributed to. Enqueued by ``POST /devices/bulk-assign`` (``backfill=True``).

Readings carry no DevEUI (only ``user_id``/``zone_id``/``value``/``timestamp``),
so a reading is attributed to the device by correlating its ``timestamp`` to the
device's raw ``lora_uplink.received_at`` rows (matched on ``dev_eui``). This is
exact when the ``lora`` zone holds a single device (the common case) and
best-effort when several devices are mixed in (sub-window timestamp collisions).
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
    device_id: int, target_user_id: int, target_zone_id: int
) -> dict:
    """Move a device's past readings from the ``lora`` catch-all zone to its new
    owner's zone. Idempotent — once moved, rows no longer match the source
    filter, so re-runs are no-ops. Returns a per-table moved-row count.
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

        src = session.execute(
            text(
                "SELECT id AS zone_id, user_id FROM analytics_zone "
                "WHERE name = 'lora' ORDER BY id LIMIT 1"
            )
        ).first()
        if src is None:
            return {"device_id": device_id, "moved": {}, "skipped": "no_lora_zone"}

        params = {
            "dev_eui": dev_eui,
            "src_zone": src.zone_id,
            "src_user": src.user_id,
            "tu": target_user_id,
            "tz": target_zone_id,
            "win": f"{_CORRELATION_WINDOW_SECONDS} seconds",
        }
        # Same predicate for the dry-run count and the move (bound params only).
        predicate = (
            "r.zone_id = :src_zone AND r.user_id = :src_user "
            "AND EXISTS (SELECT 1 FROM lora_uplink lu WHERE lu.dev_eui = :dev_eui "
            "AND lu.received_at BETWEEN r.timestamp - CAST(:win AS interval) "
            "AND r.timestamp + CAST(:win AS interval))"
        )

        moved: dict[str, int] = {}
        for table in _LORA_SENSOR_TABLES:
            n = session.execute(
                text(f"SELECT count(*) FROM {table} r WHERE {predicate}"), params
            ).scalar_one()
            log.info(
                "backfill dev_eui=%s table=%s rows=%s -> user=%s zone=%s",
                dev_eui,
                table,
                n,
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
        return {"device_id": device_id, "dev_eui": dev_eui, "moved": moved}
