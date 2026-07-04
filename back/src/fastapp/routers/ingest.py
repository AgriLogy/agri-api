"""fastapp /ingest — device-webhook ingest (Bivocom, ChirpStack, weather).

Strangler port of the django-ninja device routers, mounted (in ``agriapi.api``)
at:

* ``POST /ingest/bivocom``            (apps/bivocom/router.py)
* ``POST /ingest/lorawan/chirpstack`` (apps/lorawan/chirpstack/router.py)
* ``POST /ingest/weather``            (apps/sensors/router_weather_ingest.py)
* ``POST /ingest/sensor``             (apps/sensors/router_weather_ingest.py)

Auth: NONE. These are unauthenticated device webhooks — the gateway/bridge
authenticates by shared network/secret, NOT a JWT (matching the Django
``auth=None`` routes). Do not add ``get_current_user`` here.

Readings persist through the agri-core SQLAlchemy session (no Django ORM);
new readings push through ``fastapp.ingest.dispatch_alerts_for_reading`` so
threshold-alert emails/SMS/WhatsApp fire exactly as on the Django path.
"""

from __future__ import annotations

import datetime
import json as _json
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from agri.core.alerts import SENSOR_KEY_REGISTRY
from agri.core.database import session_scope
from fastapp import ingest
from fastapp.json import DjangoStyleJSONResponse

router = APIRouter(tags=["ingest"])
log = logging.getLogger("fastapp.ingest")

_INGEST_SKIP_KEYS: set[str] = {"npk"}


# ---------------------------------------------------------------------------
# Bivocom — POST /ingest/bivocom
# ---------------------------------------------------------------------------
# NB: the ninja route is currently a STUB (validate + log + 202, no persist /
# no dispatch — the Phase-6.5 adapter handoff is still a TODO there). This port
# is byte-parity with that stub; when the Django side starts persisting, port
# the write here too.
class BivocomUplink(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: str = Field(min_length=1, max_length=64)
    timestamp: datetime.datetime
    rssi: float | None = None
    tags: dict[str, float] = Field(min_length=1)


@router.post("/ingest/bivocom", status_code=202, summary="Bivocom gateway uplink")
def bivocom_uplink(payload: BivocomUplink):
    log.info(
        "bivocom.uplink",
        extra={
            "device_id": payload.device_id,
            "tag_count": len(payload.tags),
            "rssi": payload.rssi,
        },
    )
    return DjangoStyleJSONResponse(
        {
            "accepted": True,
            "device_id": payload.device_id,
            "tag_count": len(payload.tags),
        },
        status_code=202,
    )


# ---------------------------------------------------------------------------
# ChirpStack v4 — POST /ingest/lorawan/chirpstack
# ---------------------------------------------------------------------------
class _CSDeviceInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    devEui: str = Field(min_length=16, max_length=16)
    deviceName: str | None = None


class _CSRxInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rssi: float | None = None
    snr: float | None = None


class _CSTxInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    frequency: int | None = None


class ChirpStackUplink(BaseModel):
    model_config = ConfigDict(extra="ignore")

    deviceInfo: _CSDeviceInfo
    rxInfo: list[_CSRxInfo] = Field(default_factory=list)
    txInfo: _CSTxInfo | None = None
    fPort: int | None = None
    fCnt: int | None = None
    data: str | None = None
    object: dict = Field(default_factory=dict)


@router.post("/ingest/lorawan/chirpstack", summary="ChirpStack v4 uplink webhook")
def chirpstack_uplink(payload: ChirpStackUplink):
    dev_eui = payload.deviceInfo.devEui
    ph = ingest.decode_ph(payload.object, f_port=payload.fPort, data=payload.data)
    battery = ingest.decode_battery(payload.object)
    rssi = payload.rxInfo[0].rssi if payload.rxInfo else None
    log.info(
        "chirpstack.uplink",
        extra={
            "dev_eui": dev_eui,
            "fPort": payload.fPort,
            "ph": ph,
            "battery_v": battery,
            "rssi": rssi,
        },
    )

    with session_scope(commit=True) as session:
        # Store the complete uplink (every field) for EVERY frame — nothing dropped.
        ingest.store_lora_uplink(
            session,
            dev_eui=dev_eui,
            device_name=payload.deviceInfo.deviceName or "",
            f_cnt=payload.fCnt,
            f_port=payload.fPort,
            rssi=(payload.rxInfo[0].rssi if payload.rxInfo else None),
            snr=(payload.rxInfo[0].snr if payload.rxInfo else None),
            frequency=(payload.txInfo.frequency if payload.txInfo else None),
            battery_v=battery,
            ph=ph,
            decoded=payload.object or {},
            raw_b64=payload.data or "",
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

        if not readings:
            return DjangoStyleJSONResponse(
                {"accepted": True, "devEui": dev_eui, "channels": 0},
                status_code=202,
            )

        zone = ingest.ensure_lora_zone(session)
        now = ingest._now()
        for sensor_key, value in readings:
            ingest.write_reading(
                session,
                sensor_key=sensor_key,
                user_id=zone.user_id,
                zone_id=zone.id,
                value=value,
                timestamp=now,
            )
            # Alert dispatch must never abort the ingest loop.
            try:
                ingest.dispatch_alerts_for_reading(
                    session,
                    sensor_key=sensor_key,
                    zone_id=zone.id,
                    user_id=zone.user_id,
                    value=value,
                    timestamp=now,
                )
            except Exception:
                log.exception("alert dispatch failed for %s in lora zone", sensor_key)

        return DjangoStyleJSONResponse(
            {"accepted": True, "devEui": dev_eui, "channels": len(readings)},
            status_code=201,
        )


# ---------------------------------------------------------------------------
# Weather multi-sensor ingest — POST /ingest/weather
# ---------------------------------------------------------------------------
@router.post("/ingest/weather", summary="Multi-sensor weather ingest webhook")
async def weather_ingest(request: Request):
    raw = await request.body()
    try:
        payload = _json.loads(raw or b"{}")
    except ValueError:
        return DjangoStyleJSONResponse(
            {"error": "Expected a JSON object"}, status_code=400
        )
    if not isinstance(payload, dict):
        return DjangoStyleJSONResponse(
            {"error": "Expected a JSON object"}, status_code=400
        )

    metrics = {
        key: payload[key]
        for key in SENSOR_KEY_REGISTRY
        if key in payload and payload[key] is not None and key not in _INGEST_SKIP_KEYS
    }

    if not metrics:
        return {"inserted": 0, "detail": "all_metrics_none"}

    client = payload.get("client")
    if not client or not isinstance(client, str):
        return DjangoStyleJSONResponse(
            {"error": "client is required when any metric is provided"},
            status_code=400,
        )
    client = client.strip()

    with session_scope(commit=True) as session:
        user = ingest.user_by_username(session, client)
        if not user:
            return DjangoStyleJSONResponse(
                {"error": f"User not found for client '{client}'"}, status_code=400
            )
        zone = ingest.first_zone_for(session, user.id)
        if not zone:
            return DjangoStyleJSONResponse(
                {"error": f"No zone found for user '{user.username}'"},
                status_code=400,
            )

        now = ingest._now()
        inserted = 0
        for sensor_key, value in metrics.items():
            ingest.write_reading(
                session,
                sensor_key=sensor_key,
                user_id=user.id,
                zone_id=zone.id,
                value=value,
                timestamp=now,
            )
            inserted += 1
            try:
                ingest.dispatch_alerts_for_reading(
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

    return DjangoStyleJSONResponse({"inserted": inserted}, status_code=201)


# ---------------------------------------------------------------------------
# Single-sensor ingest — POST /ingest/sensor
# ---------------------------------------------------------------------------
class SensorReadingIn(BaseModel):
    client: str
    sensor_key: str
    value: float
    timestamp: datetime.datetime | None = None


@router.post("/ingest/sensor", summary="Single-sensor ingest webhook")
def sensor_ingest(payload: SensorReadingIn):
    sensor_key = (payload.sensor_key or "").strip()
    if sensor_key not in SENSOR_KEY_REGISTRY or sensor_key in _INGEST_SKIP_KEYS:
        return DjangoStyleJSONResponse(
            {"error": f"Unknown or unsupported sensor_key '{sensor_key}'"},
            status_code=400,
        )

    client = (payload.client or "").strip()
    if not client:
        return DjangoStyleJSONResponse({"error": "client is required"}, status_code=400)

    with session_scope(commit=True) as session:
        user = ingest.user_by_username(session, client)
        if not user:
            return DjangoStyleJSONResponse(
                {"error": f"User not found for client '{client}'"}, status_code=400
            )
        zone = ingest.first_zone_for(session, user.id)
        if not zone:
            return DjangoStyleJSONResponse(
                {"error": f"No zone found for user '{user.username}'"},
                status_code=400,
            )

        ts = payload.timestamp or ingest._now()
        ingest.write_reading(
            session,
            sensor_key=sensor_key,
            user_id=user.id,
            zone_id=zone.id,
            value=payload.value,
            timestamp=ts,
        )
        try:
            ingest.dispatch_alerts_for_reading(
                session,
                sensor_key=sensor_key,
                zone_id=zone.id,
                user_id=user.id,
                value=payload.value,
                timestamp=ts,
            )
        except Exception:
            log.exception(
                "alert dispatch failed for sensor_key=%s user=%s zone=%s",
                sensor_key,
                user.username,
                zone.id,
            )

    return DjangoStyleJSONResponse(
        {"inserted": 1, "sensor_key": sensor_key}, status_code=201
    )
