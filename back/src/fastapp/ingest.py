"""Device-ingest support for the fastapp sidecar (F9-ingest).

Pure-SQLAlchemy port of the Django device-webhook write paths
(``apps/bivocom/router.py``, ``apps/lorawan/chirpstack/router.py``,
``apps/sensors/router_weather_ingest.py``). No Django ORM — readings are
persisted through the shared agri-core SQLAlchemy session, and alert
dispatch enqueues the SAME Celery tasks (by name, same kwargs) the Django
``dispatch_alerts_for_reading`` used, via ``fastapp.celery.send_task``.

Kept here (not in agri-core) because the Django adapter lives in
``apps/alerts/engine.py`` — agri-core owns only the pure evaluator
(``agri.core.alerts``). When a second consumer needs push-dispatch, lift
``dispatch_alerts_for_reading`` into agri-core.
"""

from __future__ import annotations

import base64
import datetime
import logging
from typing import Any

from sqlalchemy import BigInteger, DateTime, Double, Integer, String, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from agri.core.alerts import (
    SENSOR_KEY_REGISTRY,
    AlertSpec,
    db_model_for,
    evaluate_alert,
)
from agri.db.analytics import (
    AnalyticsAlert,
    AnalyticsNotificationzonesensor,
    AnalyticsZone,
)
from agri.db.users import CustomUserCustomuser
from fastapp import celery

log = logging.getLogger("fastapp.ingest")


# ---------------------------------------------------------------------------
# Alert-email throttling — mirror of agriapi.settings.base (env-invariant
# domain config). Kept verbatim so the fastapp grace gate matches Django's
# byte-for-byte; a drift here would change dispatch cadence after cutover.
# ---------------------------------------------------------------------------
DEFAULT_ALERT_GRACE_PERIOD = 1800  # 30 minutes
ALERT_GRACE_PERIODS: dict[str, int] = {
    "water_flow": 5 * 60,
    "water_level": 5 * 60,
    "water_pressure": 5 * 60,
    "water_ec": 5 * 60,
    "ph_water": 5 * 60,
    "wind_speed": 15 * 60,
    "wind_direction": 15 * 60,
    "temperature_weather": 30 * 60,
    "humidity_weather": 30 * 60,
    "pressure_weather": 30 * 60,
    "solar_radiation": 30 * 60,
    "precipitation_rate": 30 * 60,
    "et0_weather": 60 * 60,
    "et0_calculated": 60 * 60,
    "soil_moisture_low": 60 * 60,
    "soil_moisture_medium": 60 * 60,
    "soil_moisture_high": 60 * 60,
    "multi_depth_soil_moisture_sensor": 60 * 60,
    "soil_temperature_low": 2 * 60 * 60,
    "soil_temperature_medium": 2 * 60 * 60,
    "soil_temperature_high": 2 * 60 * 60,
    "ec_soil_low": 2 * 60 * 60,
    "ec_soil_medium": 2 * 60 * 60,
    "ec_soil_high": 2 * 60 * 60,
    "ph_soil": 2 * 60 * 60,
    "soil_conductivity": 2 * 60 * 60,
    "soil_salinity": 2 * 60 * 60,
    "ec_salinity": 2 * 60 * 60,
    "npk": 4 * 60 * 60,
    "leaf_moisture": 30 * 60,
    "leaf_temperature": 30 * 60,
    "fruit_size": 6 * 60 * 60,
    "large_fruit_diameter": 6 * 60 * 60,
    "electricity_consumption": 30 * 60,
}


def grace_period_seconds_for(sensor_key: str) -> int:
    """Per-sensor_key cool-down between alert emails, in seconds."""
    return int(ALERT_GRACE_PERIODS.get(sensor_key, DEFAULT_ALERT_GRACE_PERIOD))


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# lora_uplink — unmanaged, append-only raw-uplink record.
#
# agri.db does NOT model this table (Django ``apps.lorawan.chirpstack.models
# .LoraUplink`` owns it, managed=False in prod / created out-of-band). We map a
# minimal SQLAlchemy model on a private Base (kept out of AgriBase.metadata so
# it never touches Alembic autogenerate) purely to INSERT the same columns.
# ---------------------------------------------------------------------------
class _IngestBase(DeclarativeBase):
    pass


class LoraUplinkRow(_IngestBase):
    __tablename__ = "lora_uplink"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dev_eui: Mapped[str] = mapped_column(String(16))
    device_name: Mapped[str] = mapped_column(String(128), default="")
    received_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    f_cnt: Mapped[int | None] = mapped_column(Integer)
    f_port: Mapped[int | None] = mapped_column(Integer)
    rssi: Mapped[float | None] = mapped_column(Double(53))
    snr: Mapped[float | None] = mapped_column(Double(53))
    frequency: Mapped[int | None] = mapped_column(BigInteger)
    battery_v: Mapped[float | None] = mapped_column(Double(53))
    ph: Mapped[float | None] = mapped_column(Double(53))
    decoded: Mapped[dict] = mapped_column(JSONB, default=dict)
    raw_b64: Mapped[str] = mapped_column(String(512), default="")


# ---------------------------------------------------------------------------
# ChirpStack RS485-LB decode — pure, ported verbatim from the Django router.
# ---------------------------------------------------------------------------
LORA_ZONE_NAME = "lora"
LORA_USER_NAME = "lora"

_PH_SCALE = 100.0
_STATUS_FPORT = 5  # RS485-LB device-status frame — carries no measurement


def decode_ph(obj: dict, *, f_port: int | None, data: str | None) -> float | None:
    """Extract the pH value from an RS485-LB uplink (codec field first, then
    raw modbus bytes). Byte-parity with the Django ``_decode_ph``."""
    obj = obj or {}
    for key in ("pH", "ph", "PH", "soil_ph", "ph_soil"):
        value = obj.get(key)
        if isinstance(value, (int, float)):
            ph = round(float(value), 2)
            return ph if 0.0 <= ph <= 14.0 else None

    if f_port == _STATUS_FPORT or not data:
        return None
    try:
        raw = base64.b64decode(data)
    except (ValueError, TypeError):
        return None
    if len(raw) < 5:
        return None
    ph = ((raw[3] << 8) | raw[4]) / _PH_SCALE
    return round(ph, 2) if 0.0 <= ph <= 14.0 else None


def decode_battery(obj: dict) -> float | None:
    """Battery voltage — ``BatV`` on data frames, ``BAT`` on status frames."""
    obj = obj or {}
    for key in ("BatV", "BAT", "battery", "batV"):
        value = obj.get(key)
        if isinstance(value, (int, float)) and 0.0 < float(value) < 20.0:
            return round(float(value), 3)
    return None


# ---------------------------------------------------------------------------
# User / zone resolution (SQLAlchemy port of the Django ``.filter().first()``
# and ``get_or_create`` calls).
# ---------------------------------------------------------------------------
def user_by_username(session: Session, username: str) -> CustomUserCustomuser | None:
    return session.scalars(
        select(CustomUserCustomuser)
        .where(CustomUserCustomuser.username == username)
        .order_by(CustomUserCustomuser.id)
        .limit(1)
    ).first()


def first_zone_for(session: Session, user_id: int) -> AnalyticsZone | None:
    return session.scalars(
        select(AnalyticsZone)
        .where(AnalyticsZone.user_id == user_id)
        .order_by(AnalyticsZone.id)
        .limit(1)
    ).first()


def ensure_lora_zone(session: Session) -> AnalyticsZone:
    """Resolve (and lazily provision) the dedicated ``lora`` zone + owner —
    ORM-agnostic mirror of the Django ``_lora_zone`` get_or_create pair."""
    user = user_by_username(session, LORA_USER_NAME)
    if user is None:
        # Django CustomUser field defaults for a get_or_create with only email
        # overridden (see apps/users/models.py). notify_every / preferred_language
        # carry DB server_defaults → omitted so Postgres fills them.
        user = CustomUserCustomuser(
            username=LORA_USER_NAME,
            email="lora@local.invalid",
            password="",
            is_superuser=False,
            firstname="",
            lastname="",
            payement_status="actif",
            is_active=True,
            is_staff=False,
            is_technician=False,
            date_joined=_now(),
        )
        session.add(user)
        session.flush()

    zone = session.scalars(
        select(AnalyticsZone)
        .where(AnalyticsZone.name == LORA_ZONE_NAME)
        .order_by(AnalyticsZone.id)
        .limit(1)
    ).first()
    if zone is None:
        # Django Zone field defaults (apps/irrigation/models.py); the get_or_create
        # overrides only space + critical_moisture_threshold.
        zone = AnalyticsZone(
            name=LORA_ZONE_NAME,
            user_id=user.id,
            space=1.0,
            critical_moisture_threshold=20.0,
            soil_param_TAW=50.0,
            soil_param_FC=50.0,
            soil_param_WP=50.0,
            soil_param_RAW=50.0,
            pomp_flow_rate=100.0,
            irrigation_water_quantity=100.0,
            elevation_m=0.0,
        )
        session.add(zone)
        session.flush()
    return zone


def sensor_model_for(sensor_key: str):
    """agri.db model class for a registry sensor_key (mirror of the Django
    adapter's ``get_sensor_model``)."""
    return db_model_for(sensor_key)


def write_reading(
    session: Session,
    *,
    sensor_key: str,
    user_id: int,
    zone_id: int,
    value: float,
    timestamp: datetime.datetime,
):
    """INSERT one sensor reading row (user/zone/value/timestamp)."""
    model = sensor_model_for(sensor_key)
    row = model(user_id=user_id, zone_id=zone_id, value=value, timestamp=timestamp)
    session.add(row)
    session.flush()
    return row


def store_lora_uplink(
    session: Session,
    *,
    dev_eui: str,
    device_name: str,
    f_cnt: int | None,
    f_port: int | None,
    rssi: float | None,
    snr: float | None,
    frequency: int | None,
    battery_v: float | None,
    ph: float | None,
    decoded: dict,
    raw_b64: str,
) -> None:
    """Persist the complete uplink — append-only (mirror of ``_store_uplink``)."""
    session.add(
        LoraUplinkRow(
            dev_eui=dev_eui,
            device_name=device_name or "",
            received_at=_now(),
            f_cnt=f_cnt,
            f_port=f_port,
            rssi=rssi,
            snr=snr,
            frequency=frequency,
            battery_v=battery_v,
            ph=ph,
            decoded=decoded or {},
            raw_b64=raw_b64 or "",
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# Push-on-ingest alert dispatch — SQLAlchemy port of
# ``apps.alerts.engine.dispatch_alerts_for_reading``. Same matching, same
# atomic grace claim, same per-channel/digest Celery enqueue (by task name).
# ---------------------------------------------------------------------------
def dispatch_alerts_for_reading(
    session: Session,
    *,
    sensor_key: str,
    zone_id: int | None,
    user_id: int,
    value: float | None,
    timestamp: datetime.datetime,
) -> int:
    if value is None:
        return 0
    if sensor_key not in SENSOR_KEY_REGISTRY:
        return 0

    # Custom notification zones (agrilogy-front #57): a notification-zone
    # sensor assignment feeding this (sensor_key, source_zone) — source_zone
    # NULL = any zone.
    nz_where = [AnalyticsNotificationzonesensor.sensor_key == sensor_key]
    if zone_id is not None:
        nz_where.append(
            (AnalyticsNotificationzonesensor.source_zone_id == zone_id)
            | (AnalyticsNotificationzonesensor.source_zone_id.is_(None))
        )
    else:
        nz_where.append(AnalyticsNotificationzonesensor.source_zone_id.is_(None))
    matching_nz_ids = list(
        session.scalars(
            select(AnalyticsNotificationzonesensor.notification_zone_id).where(
                *nz_where
            )
        ).all()
    )

    # True user-wide alerts: no farm zone AND no notification zone.
    match = (AnalyticsAlert.zone_id.is_(None)) & (
        AnalyticsAlert.notification_zone_id.is_(None)
    )
    if zone_id is not None:
        match = match | (
            (AnalyticsAlert.zone_id == zone_id)
            & (AnalyticsAlert.notification_zone_id.is_(None))
        )
    if matching_nz_ids:
        match = match | (AnalyticsAlert.notification_zone_id.in_(matching_nz_ids))

    alerts = session.scalars(
        select(AnalyticsAlert)
        .where(
            AnalyticsAlert.user_id == user_id,
            AnalyticsAlert.sensor_key == sensor_key,
            AnalyticsAlert.is_active.is_(True),
        )
        .where(match)
        .order_by(AnalyticsAlert.id)
    ).all()

    now_ts = _now()
    default_grace = grace_period_seconds_for(sensor_key)
    enqueued = 0
    email_alert_ids: list[int] = []
    value_f = float(value)
    ts_iso = timestamp.isoformat()

    for alert in alerts:
        if not evaluate_alert(
            AlertSpec(condition=alert.condition, threshold=float(alert.condition_nbr)),
            value_f,
        ):
            continue

        # An alert with no delivery channel would win the grace claim and
        # stamp the cadence for nothing — skip before the claim.
        if not (
            bool(alert.notify_email)
            or bool(alert.notify_whatsapp)
            or bool(alert.notify_sms)
        ):
            continue

        override = alert.grace_override_seconds
        grace_seconds = override if override is not None else default_grace
        cutoff = now_ts - datetime.timedelta(seconds=grace_seconds)

        # Atomic conditional claim: only the row whose last_emailed_at actually
        # flipped enqueues (a burst of simultaneous readings can't double-send).
        won = session.execute(
            update(AnalyticsAlert)
            .where(
                AnalyticsAlert.id == alert.id,
                (AnalyticsAlert.last_emailed_at.is_(None))
                | (AnalyticsAlert.last_emailed_at < cutoff),
            )
            .values(last_emailed_at=now_ts, last_triggered_at=now_ts)
        ).rowcount
        if not won:
            continue

        if bool(alert.notify_email):
            email_alert_ids.append(alert.id)
        if bool(alert.notify_whatsapp):
            celery.send_task(
                "agriapi.tasks.send_alert_whatsapp",
                alert_id=alert.id,
                value=value_f,
                timestamp_iso=ts_iso,
            )
        if bool(alert.notify_sms):
            celery.send_task(
                "agriapi.tasks.send_alert_sms",
                alert_id=alert.id,
                value=value_f,
                timestamp_iso=ts_iso,
            )
        enqueued += 1

    # Aggregation digest (#37): >1 email alert on this reading → ONE combined
    # email; a single alert keeps the original per-alert email.
    if email_alert_ids:
        if len(email_alert_ids) == 1:
            celery.send_task(
                "agriapi.tasks.send_alert_email",
                alert_id=email_alert_ids[0],
                value=value_f,
                timestamp_iso=ts_iso,
            )
        else:
            celery.send_task(
                "agriapi.tasks.send_alert_digest_email",
                alert_ids=email_alert_ids,
                value=value_f,
                timestamp_iso=ts_iso,
            )

    return enqueued


__all__ = [
    "ALERT_GRACE_PERIODS",
    "DEFAULT_ALERT_GRACE_PERIOD",
    "LoraUplinkRow",
    "decode_battery",
    "decode_ph",
    "dispatch_alerts_for_reading",
    "ensure_lora_zone",
    "first_zone_for",
    "grace_period_seconds_for",
    "sensor_model_for",
    "store_lora_uplink",
    "user_by_username",
    "write_reading",
]
