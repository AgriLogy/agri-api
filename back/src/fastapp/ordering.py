"""Shared ORDER BY for "the latest reading" of a sensor table."""

from __future__ import annotations


def latest_first(model: type) -> tuple:
    """Newest first, deterministic when several rows share a timestamp.

    Historical data holds parallel streams saved without a device id (up to 11
    rows at one timestamp, with different values). Rows that carry a device
    come first, then the most recently stored, so the same question always
    reads the same row.
    """
    return (
        model.timestamp.desc(),
        model.device_id.is_(None),
        model.id.desc(),
    )
