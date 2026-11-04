"""SQLAlchemy Core mirrors of the farm-map schema (agri-db ``c9e1a2b3d4f5``).

agri-db owns these tables (``agri.db.farm_map`` + the sector crop columns on
``AnalyticsSector``), but agri-api only sees agri-db through the agri-core pin,
which predates them. Rather than block on an agri-db -> agri-core -> agri-api
release chain, the farm-map router reads and writes through these Core
``Table`` objects — the same approach ``selfreads`` uses for the device
coordinate columns. Swap to the ORM models once the agri-core pin carries an
agri-db release that includes ``agri.db.farm_map``.

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

#: Only the columns added by c9e1a2b3d4f5 (plus the keys needed to address a
#: row). Everything else on analytics_sector stays with the ORM model.
sector_meta = Table(
    "analytics_sector",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, nullable=False),
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
