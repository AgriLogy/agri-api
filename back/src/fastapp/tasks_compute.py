"""fastapp compute/scan task bodies (F8b) — the periodic beat jobs.

Django-free ports of the compute + scan Celery tasks in ``agriapi/tasks.py``.
Plain functions here; the native Celery app (F10) wraps them under the SAME
names (``agriapi.tasks.<name>``) with the static beat schedule. Additive until
then — the Django worker keeps running everything.

All physics lives in agri-core (``agri.core.agronomy``); this module only reads
zones and upserts results via the agri-core SQLAlchemy session.
"""

from __future__ import annotations

import logging

from agri.core.agronomy import compute_et0_for_zone
from agri.core.database import session_scope
from agri.db.analytics import (
    AnalyticsEt0calculated,
    AnalyticsVpdweather,
    AnalyticsZone,
)
from sqlalchemy import select

logger = logging.getLogger(__name__)


def _upsert_value(session, model, *, zone_id: int, timestamp, user_id, value) -> None:
    """update_or_create by (zone, timestamp) — keep exactly one row per bucket."""
    row = session.execute(
        select(model).where(model.zone_id == zone_id, model.timestamp == timestamp)
    ).scalar_one_or_none()
    if row is not None:
        row.value = value
        row.user_id = user_id
    else:
        session.add(
            model(zone_id=zone_id, timestamp=timestamp, user_id=user_id, value=value)
        )


def compute_et0_vpd_hourly() -> dict:
    """For each zone, ask agri-core for one hour of ET0 + VPD and persist it.

    Idempotent per (zone, timestamp): ``update_or_create`` keeps exactly one row
    per hour bucket so retries / double-fires don't accumulate duplicates.
    """
    with session_scope(commit=True) as session:
        zone_ids = list(session.scalars(select(AnalyticsZone.id)).all())
        written = 0
        for zid in zone_ids:
            result = compute_et0_for_zone(session, zid)
            if result is None:
                continue
            _upsert_value(
                session,
                AnalyticsEt0calculated,
                zone_id=zid,
                timestamp=result.timestamp,
                user_id=result.user_id,
                value=result.et0_mm_per_h,
            )
            _upsert_value(
                session,
                AnalyticsVpdweather,
                zone_id=zid,
                timestamp=result.timestamp,
                user_id=result.user_id,
                value=result.vpd_kpa,
            )
            written += 1
    return {
        "zones_processed": len(zone_ids),
        "et0_rows": written,
        "vpd_rows": written,
    }
