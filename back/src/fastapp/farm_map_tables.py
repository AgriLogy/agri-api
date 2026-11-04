"""SQLAlchemy Core mirrors of the farm-map schema (agri-db ``c9e1a2b3d4f5``).

agri-db owns these tables (``agri.db.farm_map`` + the sector crop columns on
``AnalyticsSector``). The farm-map router was written while agri-api's pin
still resolved agri-db 0.19.0, so it reads and writes through these Core
``Table`` objects. Since agri-core 0.27.1 the pin resolves agri-db 0.23.1,
which ships the ORM models too; the mirrors stay correct (same columns) and
can be swapped for ``agri.db.farm_map`` in a follow-up.

Kept on a private ``MetaData`` so nothing here can leak into ``create_all`` or
Alembic autogenerate for the shared metadata.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

#: The analytics_sector shape (b8c2f0d5e713) and crop (c9e1a2b3d4f5) columns.
#: Read/written through Core so sectors work regardless of which agri-db the
#: ORM resolves (with agri-db < 0.20.0 the ORM lacked them and /sectors 500'd).
sector_meta = Table(
    "analytics_sector",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("geometry", JSONB),
    Column("area_ha", Float),
    Column("perimeter_m", Float),
    Column("color", String(9)),
    Column("geometry_updated_at", DateTime(timezone=True)),
    Column("crop", String(80)),
    Column("variety", String(80)),
    Column("planting_date", Date),
    Column("irrigation_type", String(30)),
    Column("root_depth_cm", Float),
    Column("is_active", Boolean, nullable=False),
)

SECTOR_META_COLUMNS = (
    "crop",
    "variety",
    "planting_date",
    "irrigation_type",
    "root_depth_cm",
    "is_active",
)

sensor_installation = Table(
    "analytics_sensorinstallation",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("sector_id", BigInteger),
    Column("zone_id", BigInteger),
    Column("device_id", BigInteger),
    Column("name", String(100), nullable=False),
    Column("sensor_type", String(40), nullable=False),
    Column("sensor_key", String(64)),
    Column("latitude", Float, nullable=False),
    Column("longitude", Float, nullable=False),
    Column("installation_depth_cm", Float),
    Column("model", String(100)),
    Column("serial_number", String(128)),
    Column("node_id", String(128)),
    Column("installation_date", Date),
    Column("calibration_profile", String(100)),
    Column("notes", Text),
    Column("maintenance_status", String(20)),
    Column("client_ref", String(64)),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)

map_preference = Table(
    "analytics_mappreference",
    metadata,
    Column("user_id", BigInteger, primary_key=True),
    Column("basemap", String(30), nullable=False),
    Column("layers", JSONB, nullable=False),
    Column("camera", JSONB),
    Column("updated_at", DateTime(timezone=True)),
)

SENSOR_INSTALLATION_TABLE = "analytics_sensorinstallation"
MAP_PREFERENCE_TABLE = "analytics_mappreference"
