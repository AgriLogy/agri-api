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
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
from agri.db.devices import AnalyticsDevice
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


def parse_timestamp(value: Any) -> datetime.datetime | None:
    """Best-effort parse of a reading timestamp from an untyped source (an MQTT
    JSON body). ``None``/empty → ``None`` (caller falls back to ``_now()``); an
    ISO-8601 string (``Z`` allowed) → aware datetime; a datetime passes through.
    A malformed string returns ``None`` rather than failing the whole message.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


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
# ``analytics_device.device_type`` value for a LoRaWAN device (serial == DevEUI).
LORA_DEVICE_TYPE = "lora"

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
        # overridden (see apps/users/models.py). notify_every (240) and
        # preferred_language ('fr') are set EXPLICITLY to the Django app-level
        # defaults: the agri.db model declares server_defaults, but a Django-
        # migration-built schema (e.g. the test DB) has none, so relying on the
        # DB to fill them raises NotNullViolation on the first lora uplink over a
        # Django-only ingest path (HTTP had Django create this row first). These
        # values match what the Django get_or_create writes, byte-for-byte.
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
            notify_every=240,
            preferred_language="fr",
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


def auto_register_lora_device(
    session: Session, dev_eui: str, device_name: str = ""
) -> None:
    """Lazily create an ``analytics_device`` row for a never-before-seen LoRa
    DevEUI so it surfaces in the admin device list as *unassigned* (owned by the
    ``lora`` placeholder user, ``zone_id`` NULL) ready for an admin to attribute.

    Idempotent on the unique ``serial`` constraint via ``ON CONFLICT DO NOTHING``
    (a caught IntegrityError would poison the surrounding transaction, so the
    conflict is handled in SQL instead).
    """
    lora_zone = ensure_lora_zone(session)  # guarantees the ``lora`` owner exists
    session.execute(
        pg_insert(AnalyticsDevice)
        .values(
            device_type=LORA_DEVICE_TYPE,
            serial=dev_eui,
            name=(device_name or dev_eui)[:120],
            user_id=lora_zone.user_id,
            zone_id=None,
            is_active=True,
            created_at=_now(),
        )
        .on_conflict_do_nothing(index_elements=["serial"])
    )
    session.flush()


def resolve_device(
    session: Session, dev_eui: str, *, device_name: str = ""
) -> tuple[int | None, int | None, int | None]:
    """Resolve ``(device_id, user_id, zone_id)`` for a LoRaWAN device from the
    ``analytics_device`` attribution table (``serial`` == DevEUI).

    ``device_id`` is ALWAYS returned (an unknown DevEUI is auto-registered under
    the ``lora`` placeholder first) so every device reading can be stamped with
    it — ownership then follows the device on transfer, no reading rewrite.

    ``user_id`` / ``zone_id`` are the device's owner only when it is active AND
    assigned (``zone_id`` set); otherwise ``(None, None)`` → the caller routes to
    the shared ``lora`` catch-all. Existence is keyed on the unique ``serial``
    (not filtered by ``is_active``) so an inactive device is never re-inserted.
    """
    device = session.scalars(
        select(AnalyticsDevice)
        .where(AnalyticsDevice.serial == dev_eui)
        .order_by(AnalyticsDevice.id)
        .limit(1)
    ).first()
    if device is None:
        auto_register_lora_device(session, dev_eui, device_name)
        device = session.scalars(
            select(AnalyticsDevice)
            .where(AnalyticsDevice.serial == dev_eui)
            .order_by(AnalyticsDevice.id)
            .limit(1)
        ).first()
    if device is None:  # pragma: no cover - insert raced away
        return None, None, None
    if device.is_active and device.zone_id is not None:
        return device.id, device.user_id, device.zone_id
    return device.id, None, None


def resolve_device_zone(
    session: Session, dev_eui: str, *, device_name: str = ""
) -> tuple[int, int] | None:
    """Back-compat shim over :func:`resolve_device`: returns ``(user_id,
    zone_id)`` when the device is active+assigned, else ``None`` (→ lora)."""
    _, user_id, zone_id = resolve_device(session, dev_eui, device_name=device_name)
    return (user_id, zone_id) if user_id is not None and zone_id is not None else None


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
    device_id: int | None = None,
):
    """INSERT one sensor reading row. ``device_id`` (when the reading came from a
    registered hardware device) stamps the row so ownership can later be resolved
    by JOIN to ``analytics_device``; ``user_id``/``zone_id`` are still written as
    the resolved-at-ingest snapshot (fallback for non-device readings)."""
    model = sensor_model_for(sensor_key)
    row = model(
        user_id=user_id,
        zone_id=zone_id,
        value=value,
        timestamp=timestamp,
        device_id=device_id,
    )
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


# ---------------------------------------------------------------------------
# Transport-agnostic ingest handlers.
#
# The bodies below were lifted out of the HTTP router (``routers/ingest.py``)
# so a SECOND transport — the MQTT subscriber (``fastapp.mqtt``) — can persist
# byte-for-byte the same rows and enqueue the same alert tasks without going
# through FastAPI. Each ``handle_*`` takes an open ``session`` plus already-
# parsed primitives; the caller owns transaction scope (``session_scope``) and
# response/ack shaping. HTTP keeps its exact envelopes; MQTT just logs the count.
# ---------------------------------------------------------------------------
class IngestError(Exception):
    """A caller-visible ingest failure (bad client, no zone, unknown key).

    The HTTP router maps this to the SAME ``{"error": message}`` + status the
    inline code returned; the MQTT subscriber logs it and drops the message.
    """

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def resolve_user_zone(
    session: Session, client: str
) -> tuple[CustomUserCustomuser, AnalyticsZone]:
    """(user, first zone) for a client username, or raise ``IngestError`` with
    the exact message/status the weather+sensor routes returned inline."""
    user = user_by_username(session, client)
    if not user:
        raise IngestError(f"User not found for client '{client}'")
    zone = first_zone_for(session, user.id)
    if not zone:
        raise IngestError(f"No zone found for user '{user.username}'")
    return user, zone


def handle_metrics(
    session: Session,
    *,
    client: str,
    metrics: dict[str, float],
    timestamp: datetime.datetime | None = None,
) -> int:
    """Write one reading per ``{sensor_key: value}`` under the client's first
    zone and push each through alert dispatch. Returns the inserted count.

    Shared by the weather (multi-metric), single-sensor, and Bivocom paths.
    Callers pre-filter ``metrics`` to known ``sensor_key``s. Alert dispatch is
    wrapped so a failure never aborts the write loop (parity with the routers).
    """
    user, zone = resolve_user_zone(session, client)
    now = timestamp or _now()
    inserted = 0
    for sensor_key, value in metrics.items():
        write_reading(
            session,
            sensor_key=sensor_key,
            user_id=user.id,
            zone_id=zone.id,
            value=value,
            timestamp=now,
        )
        inserted += 1
        try:
            dispatch_alerts_for_reading(
                session,
                sensor_key=sensor_key,
                zone_id=zone.id,
                user_id=user.id,
                value=value,
                timestamp=now,
            )
        except Exception:
            log.exception(
                "alert dispatch failed for sensor_key=%s user=%s zone=%s",
                sensor_key,
                user.username,
                zone.id,
            )
    return inserted


def handle_chirpstack_uplink(
    session: Session,
    *,
    dev_eui: str,
    device_name: str,
    f_cnt: int | None,
    f_port: int | None,
    rssi: float | None,
    snr: float | None,
    frequency: int | None,
    obj: dict,
    data: str,
) -> int:
    """Persist a ChirpStack v4 uplink: append the raw record, then write the
    metrics present on this frame (pH / battery / signal) under the shared
    ``lora`` zone and dispatch alerts. Returns the channel (reading) count.

    Ported verbatim from ``routers.ingest.chirpstack_uplink`` so HTTP + MQTT
    share one code path (see ``test_ingest_parity``).
    """
    ph = decode_ph(obj, f_port=f_port, data=data)
    battery = decode_battery(obj)
    log.info(
        "chirpstack.uplink",
        extra={
            "dev_eui": dev_eui,
            "fPort": f_port,
            "ph": ph,
            "battery_v": battery,
            "rssi": rssi,
        },
    )

    # Store the complete uplink (every field) for EVERY frame — nothing dropped.
    store_lora_uplink(
        session,
        dev_eui=dev_eui,
        device_name=device_name or "",
        f_cnt=f_cnt,
        f_port=f_port,
        rssi=rssi,
        snr=snr,
        frequency=frequency,
        battery_v=battery,
        ph=ph,
        decoded=obj or {},
        raw_b64=data or "",
    )

    # Per-metric graphable series under the ``lora`` zone — only metrics
    # present on this frame: (sensor_key, value).
    readings: list[tuple[str, float]] = []
    if ph is not None:
        readings.append(("ph_soil", ph))
    if battery is not None:
        readings.append(("battery", battery))
    if rssi is not None:
        readings.append(("signal", round(float(rssi), 1)))

    # Attribute this device (auto-registers an unknown DevEUI) even when the
    # frame carries no graphable metric, so status-only devices still surface in
    # the admin device list. ``device_id`` is stamped on every reading so
    # ownership follows the device on transfer (resolved by JOIN in later phases).
    device_id, user_id, zone_id = resolve_device(
        session, dev_eui, device_name=device_name
    )

    if not readings:
        return 0

    if user_id is None or zone_id is None:
        # Unregistered / unassigned / inactive device → shared ``lora`` catch-all
        # (the reading still carries ``device_id`` so it follows on assignment).
        lora_zone = ensure_lora_zone(session)
        user_id, zone_id = lora_zone.user_id, lora_zone.id

    now = _now()
    for sensor_key, value in readings:
        write_reading(
            session,
            sensor_key=sensor_key,
            user_id=user_id,
            zone_id=zone_id,
            value=value,
            timestamp=now,
            device_id=device_id,
        )
        # Alert dispatch must never abort the ingest loop.
        try:
            dispatch_alerts_for_reading(
                session,
                sensor_key=sensor_key,
                zone_id=zone_id,
                user_id=user_id,
                value=value,
                timestamp=now,
            )
        except Exception:
            log.exception(
                "alert dispatch failed for %s (dev_eui=%s)", sensor_key, dev_eui
            )

    return len(readings)


__all__ = [
    "ALERT_GRACE_PERIODS",
    "DEFAULT_ALERT_GRACE_PERIOD",
    "IngestError",
    "LoraUplinkRow",
    "auto_register_lora_device",
    "decode_battery",
    "decode_ph",
    "dispatch_alerts_for_reading",
    "ensure_lora_zone",
    "first_zone_for",
    "grace_period_seconds_for",
    "handle_chirpstack_uplink",
    "handle_metrics",
    "parse_timestamp",
    "resolve_device",
    "resolve_device_zone",
    "resolve_user_zone",
    "sensor_model_for",
    "store_lora_uplink",
    "user_by_username",
    "write_reading",
]
