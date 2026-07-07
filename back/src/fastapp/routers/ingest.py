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
    rx = payload.rxInfo[0] if payload.rxInfo else None
    with session_scope(commit=True) as session:
        channels = ingest.handle_chirpstack_uplink(
            session,
            dev_eui=dev_eui,
            device_name=payload.deviceInfo.deviceName or "",
            f_cnt=payload.fCnt,
            f_port=payload.fPort,
            rssi=rx.rssi if rx else None,
            snr=rx.snr if rx else None,
            frequency=payload.txInfo.frequency if payload.txInfo else None,
            obj=payload.object or {},
            data=payload.data or "",
        )
    # No metrics on this frame (e.g. an fPort-5 status frame) → 202; else 201.
    return DjangoStyleJSONResponse(
        {"accepted": True, "devEui": dev_eui, "channels": channels},
        status_code=202 if channels == 0 else 201,
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

    try:
        with session_scope(commit=True) as session:
            inserted = ingest.handle_metrics(session, client=client, metrics=metrics)
    except ingest.IngestError as exc:
        return DjangoStyleJSONResponse({"error": exc.message}, status_code=exc.status)

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

    try:
        with session_scope(commit=True) as session:
            ingest.handle_metrics(
                session,
                client=client,
                metrics={sensor_key: payload.value},
                timestamp=payload.timestamp,
            )
    except ingest.IngestError as exc:
        return DjangoStyleJSONResponse({"error": exc.message}, status_code=exc.status)

    return DjangoStyleJSONResponse(
        {"inserted": 1, "sensor_key": sensor_key}, status_code=201
    )
