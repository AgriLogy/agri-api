"""Satellite history storage (agri-api#459 phase 3, agri-db 0.28.0).

* Index statistics and acquisition dates fetched from Copernicus are written
  to ``sector_satellite_stat`` / ``satellite_acquisition`` so trend charts read
  the database instead of spending the free-tier quota again.
* Every shape change of a sector is kept in ``sector_geometry_revision``.

Rows are keyed by :func:`geometry_hash` of the shape they were computed on:
after a redraw the old rows stay, but a read for the new shape no longer
matches them. Every function is a no-op (or returns nothing) when the schema
predates the tables, so the NDVI endpoints keep working on an un-migrated DB.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from agri.db.farm_map import (
    SatelliteAcquisition,
    SectorGeometryRevision,
    SectorSatelliteStat,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from fastapp.schema_compat import table_available

STAT_TABLE = "sector_satellite_stat"
ACQUISITION_TABLE = "satellite_acquisition"
REVISION_TABLE = "sector_geometry_revision"


def geometry_hash(geometry: dict[str, Any]) -> str:
    """Stable SHA-256 of a GeoJSON shape (key order and spacing ignored)."""
    canonical = json.dumps(geometry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _iso(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------- stats
def save_stats(
    session: Session,
    sector_id: int,
    geometry: dict[str, Any],
    index_kind: str,
    series: list[dict[str, Any]],
) -> int:
    """Upsert a fetched series (the ``/stats`` item shape). Returns rows written."""
    if not series or not table_available(session, STAT_TABLE):
        return 0
    ghash = geometry_hash(geometry)
    now = dt.datetime.now(dt.UTC)
    rows = []
    for item in series:
        day = _iso(item.get("date"))
        mean = item.get("mean")
        if day is None or not isinstance(mean, (int, float)):
            continue
        rows.append(
            {
                "sector_id": sector_id,
                "geometry_hash": ghash,
                "index_kind": index_kind,
                "acquired_on": day,
                "mean": float(mean),
                "min": item.get("min"),
                "max": item.get("max"),
                "std": item.get("std"),
                "valid_fraction": item.get("valid_fraction"),
                "classes": item.get("classes"),
                "fetched_at": now,
            }
        )
    if not rows:
        return 0
    stmt = pg_insert(SectorSatelliteStat).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="sector_satellite_stat_sector_hash_index_date_key",
        set_={
            k: stmt.excluded[k]
            for k in (
                "mean",
                "min",
                "max",
                "std",
                "valid_fraction",
                "classes",
                "fetched_at",
            )
        },
    )
    session.execute(stmt)
    return len(rows)


def stored_series(
    session: Session,
    sector_id: int,
    geometry: dict[str, Any],
    index_kind: str,
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    """Stored stats for the sector's CURRENT shape, oldest first."""
    if not table_available(session, STAT_TABLE):
        return []
    rows = session.scalars(
        select(SectorSatelliteStat)
        .where(
            SectorSatelliteStat.sector_id == sector_id,
            SectorSatelliteStat.geometry_hash == geometry_hash(geometry),
            SectorSatelliteStat.index_kind == index_kind,
            SectorSatelliteStat.acquired_on >= start,
            SectorSatelliteStat.acquired_on <= end,
        )
        .order_by(SectorSatelliteStat.acquired_on)
    ).all()
    return [
        {
            "date": r.acquired_on.isoformat(),
            "mean": r.mean,
            "min": r.min,
            "max": r.max,
            "std": r.std,
            "valid_fraction": r.valid_fraction,
            "classes": r.classes,
        }
        for r in rows
    ]


# -------------------------------------------------------------- acquisitions
def save_acquisitions(
    session: Session,
    sector_id: int,
    geometry: dict[str, Any],
    dates: list[dict[str, Any]],
) -> int:
    if not dates or not table_available(session, ACQUISITION_TABLE):
        return 0
    ghash = geometry_hash(geometry)
    rows = []
    for item in dates:
        day = _iso(item.get("date"))
        if day is None:
            continue
        cloud = item.get("cloud_cover")
        rows.append(
            {
                "sector_id": sector_id,
                "geometry_hash": ghash,
                "acquired_on": day,
                "cloud_cover_pct": float(cloud)
                if isinstance(cloud, (int, float))
                else None,
            }
        )
    if not rows:
        return 0
    stmt = pg_insert(SatelliteAcquisition).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="satellite_acquisition_sector_hash_date_key",
        set_={"cloud_cover_pct": stmt.excluded.cloud_cover_pct},
    )
    session.execute(stmt)
    return len(rows)


# ----------------------------------------------------------------- revisions
def record_revision(
    session: Session,
    *,
    sector_id: int,
    user_id: int,
    geometry: dict[str, Any] | None,
    area_ha: float | None,
    perimeter_m: float | None,
    source: str = "draw",
) -> None:
    """Keep the shape a sector now has. Skips erases and unchanged shapes."""
    if geometry is None or not table_available(session, REVISION_TABLE):
        return
    last = session.scalars(
        select(SectorGeometryRevision)
        .where(SectorGeometryRevision.sector_id == sector_id)
        .order_by(SectorGeometryRevision.id.desc())
        .limit(1)
    ).first()
    if last is not None and geometry_hash(last.geometry) == geometry_hash(geometry):
        return
    session.add(
        SectorGeometryRevision(
            sector_id=sector_id,
            user_id=user_id,
            geometry=geometry,
            area_ha=area_ha,
            perimeter_m=perimeter_m,
            source=source,
            created_at=dt.datetime.now(dt.UTC),
        )
    )


def revisions(session: Session, sector_id: int) -> list[dict[str, Any]]:
    """Newest first."""
    if not table_available(session, REVISION_TABLE):
        return []
    rows = session.scalars(
        select(SectorGeometryRevision)
        .where(SectorGeometryRevision.sector_id == sector_id)
        .order_by(SectorGeometryRevision.id.desc())
    ).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "source": r.source,
            "area_ha": r.area_ha,
            "perimeter_m": r.perimeter_m,
            "geometry": r.geometry,
        }
        for r in rows
    ]


def revision(
    session: Session, sector_id: int, revision_id: int
) -> SectorGeometryRevision | None:
    if not table_available(session, REVISION_TABLE):
        return None
    return session.scalars(
        select(SectorGeometryRevision).where(
            SectorGeometryRevision.id == revision_id,
            SectorGeometryRevision.sector_id == sector_id,
        )
    ).first()
