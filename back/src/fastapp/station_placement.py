"""Where each weather station is supposed to be: outdoors, indoors, or unknown.

The daily exposure check (``fastapp.tasks_ops.check_station_exposure``)
*detects* whether a station measures the field or sits sheltered. This module
stores what the back-office *declares* for each device, so the two can be
compared:

* ``auto``    — nothing declared (default): the detection decides, and a
                sheltered / suspect station is emailed to ops;
* ``outdoor`` — declared outdoors: same alerting, and a sheltered reading is
                flagged as a mismatch with what was declared;
* ``indoor``  — declared indoors on purpose (greenhouse, shed, test bench): the
                check still runs and is shown, but never emails.

Stored in ``analytics_systemsetting`` under :data:`SETTING_KEY` as
``{"devices": {"<device id>": {"placement", "updated_at", "updated_by"}}}``.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import select

from agri.db.audit import AnalyticsSystemsetting

SETTING_KEY = "station_placement"
SETTING_CATEGORY = "stations"

PLACEMENT_AUTO = "auto"
PLACEMENT_OUTDOOR = "outdoor"
PLACEMENT_INDOOR = "indoor"
PLACEMENTS = (PLACEMENT_AUTO, PLACEMENT_OUTDOOR, PLACEMENT_INDOOR)

# What the detection says, from the exposure status.
DETECTED_OUTDOOR = "outdoor"
DETECTED_INDOOR = "indoor"
DETECTED_UNCERTAIN = "uncertain"
DETECTED_UNKNOWN = "unknown"
_DETECTED = {
    "ok": DETECTED_OUTDOOR,
    "sheltered": DETECTED_INDOOR,
    "suspect": DETECTED_UNCERTAIN,
}


def _row(session):
    return session.scalars(
        select(AnalyticsSystemsetting).where(AnalyticsSystemsetting.key == SETTING_KEY)
    ).first()


def read_placements(session) -> dict[int, dict[str, Any]]:
    """``{device_id: {"placement", "updated_at", "updated_by"}}``. Tolerates a
    hand-edited value: anything malformed is skipped, never raised."""
    row = _row(session)
    devices = row.value.get("devices") if row and isinstance(row.value, dict) else None
    out: dict[int, dict[str, Any]] = {}
    for key, entry in (devices if isinstance(devices, dict) else {}).items():
        try:
            device_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("placement") in PLACEMENTS:
            out[device_id] = entry
    return out


def placement_of(placements: dict[int, dict[str, Any]], device_id: int) -> str:
    return (placements.get(device_id) or {}).get("placement", PLACEMENT_AUTO)


def set_placement(session, device_id: int, placement: str, *, username: str) -> dict:
    """Declare a device's placement (``auto`` clears the declaration)."""
    if placement not in PLACEMENTS:
        raise ValueError(f"placement must be one of {list(PLACEMENTS)}")
    now = datetime.datetime.now(datetime.timezone.utc)
    row = _row(session)
    value = dict(row.value) if row and isinstance(row.value, dict) else {}
    devices = dict(value.get("devices") or {})
    entry = {
        "placement": placement,
        "updated_at": now.isoformat(),
        "updated_by": username,
    }
    if placement == PLACEMENT_AUTO:
        devices.pop(str(device_id), None)
    else:
        devices[str(device_id)] = entry
    value["devices"] = devices
    if row is None:
        session.add(
            AnalyticsSystemsetting(
                key=SETTING_KEY, value=value, category=SETTING_CATEGORY, updated_at=now
            )
        )
    else:
        row.value = value
        row.updated_at = now
    session.flush()
    return entry


def detected_placement(status: str) -> str:
    return _DETECTED.get(status, DETECTED_UNKNOWN)


def is_mismatch(placement: str, detected: str) -> bool:
    """The declaration and the detection disagree."""
    if placement == PLACEMENT_OUTDOOR:
        return detected in (DETECTED_INDOOR, DETECTED_UNCERTAIN)
    if placement == PLACEMENT_INDOOR:
        return detected == DETECTED_OUTDOOR
    return False


def alerts_enabled(placement: str) -> bool:
    """A station declared indoors on purpose is never emailed about."""
    return placement != PLACEMENT_INDOOR
