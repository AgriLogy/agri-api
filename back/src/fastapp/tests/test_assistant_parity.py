"""F7 golden parity: /assistant — fastapp must match the Django ninja endpoints
it replaces (``apps/assistant/router.py``).

The deterministic surface — the tool catalog, conversation CRUD, the 404 for an
unknown tool, and the 401 — is compared BYTE-for-byte. Tool invokes that read
seeded rows (get_active_alerts, list_recent_notifications, get_farm_status) are
compared as parsed JSON (the row ids match by construction because both surfaces
read the same committed rows). ``/chat`` with no ``AI_API_KEY`` runs the
rule-based orchestrator on both sides → byte-identical envelope; a separate test
mocks the LLM ``_post`` on BOTH sides to prove the tool-calling ``/chat``
envelope matches too (the model *reply text* is the canned mock, so it is
deterministic here but is NOT byte-matchable against a live model in prod).

Both surfaces drive the SAME committed rows + the SAME Django-minted access
token: Django via DRF's APIClient (ninja mounts at the URL root), fastapp via
Starlette's TestClient.

Dual-ORM: Postgres only + committed rows (fastapp reads/writes via a separate
SQLAlchemy connection).
"""

from __future__ import annotations

import datetime
from unittest import mock

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

TOOLS = "/assistant/tools"
CHAT = "/assistant/chat"
CONV = "/assistant/conversations"


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture(autouse=True)
def _no_ai_key(monkeypatch):
    """Force the rule-based orchestrator on BOTH sides (empty AI key) so /chat
    is deterministic for byte-parity (the LLM path is exercised separately)."""
    monkeypatch.setattr(get_settings(), "ai_api_key", "")
    monkeypatch.setattr(dj_settings, "AI_API_KEY", "", raising=False)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def django() -> APIClient:
    return APIClient()


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="as-owner",
        email="as-owner@example.com",
        password="irrelevant-3921",
    )


@pytest.fixture
def technician(django_user_model):
    return django_user_model.objects.create_user(
        username="as-tech",
        email="as-tech@example.com",
        password="irrelevant-3921",
        is_technician=True,
    )


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both(fast, django, user, path, method="get", data=None):
    tok = _token(user)
    dj_kw = {"HTTP_AUTHORIZATION": f"Bearer {tok}"}
    fp_kw = {"headers": {"Authorization": f"Bearer {tok}"}}
    if data is not None:
        dj_kw["data"] = data
        dj_kw["format"] = "json"  # DRF: send the body as JSON, not multipart
        fp_kw["json"] = data
    dj = getattr(django, method)(path, **dj_kw)
    fp = getattr(fast, method)(path, **fp_kw)
    return dj, fp


def _make_zone(user, name):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=user,
        name=name,
        space=1000.0,
        critical_moisture_threshold=25.0,
    )


def _make_alert(user, **over):
    from apps.alerts.models import Alert

    payload = {
        "name": "Low soil moisture",
        "type": "Humidity",
        "description": "",
        "condition": "<",
        "condition_nbr": 20,
        "sensor_key": "soilMoisture",
        "is_active": True,
    }
    payload.update(over)
    return Alert.objects.create(user=user, **payload)


# --- deterministic surface: byte-identical ---------------------------------


def test_tool_catalog_is_byte_identical(fast, django, owner):
    dj, fp = _both(fast, django, owner, TOOLS)
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content  # full catalog, byte-for-byte


def test_unknown_tool_404_is_byte_identical(fast, django, owner):
    dj, fp = _both(
        fast, django, owner, f"{TOOLS}/nope", method="post", data={"params": {}}
    )
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content == b'{"detail": "Unknown tool: nope"}'


def test_missing_auth_is_401_on_both(fast, django):
    dj = django.get(TOOLS)
    fp = fast.get(TOOLS)
    assert dj.status_code == fp.status_code == 401


# --- conversation CRUD: byte-identical -------------------------------------


def _conv_body(**over) -> dict:
    body = {
        "title": "My chat",
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "created_at": "2026-06-18T10:00:00Z",
        "updated_at": "2026-06-18T10:00:00Z",
    }
    body.update(over)
    return body


def test_conversations_empty_list_byte_identical(fast, django, owner):
    dj, fp = _both(fast, django, owner, CONV)
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content == b"[]"


def test_conversation_upsert_and_list_byte_identical(fast, django, owner):
    dj, fp = _both(fast, django, owner, f"{CONV}/c-1", method="put", data=_conv_body())
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content  # serialized conversation, byte-for-byte

    dj2, fp2 = _both(fast, django, owner, CONV)
    assert dj2.content == fp2.content
    assert fp2.json()[0]["id"] == "c-1"
    assert fp2.json()[0]["createdAt"] == "2026-06-18T10:00:00+00:00"


def test_conversation_upsert_replaces_byte_identical(fast, django, owner):
    _both(fast, django, owner, f"{CONV}/c-1", method="put", data=_conv_body())
    dj, fp = _both(
        fast,
        django,
        owner,
        f"{CONV}/c-1",
        method="put",
        data=_conv_body(title="Renamed"),
    )
    assert dj.content == fp.content
    dj2, fp2 = _both(fast, django, owner, CONV)
    assert dj2.content == fp2.content
    assert len(fp2.json()) == 1 and fp2.json()[0]["title"] == "Renamed"


def test_conversation_delete_byte_identical(fast, django, owner):
    _both(fast, django, owner, f"{CONV}/c-1", method="put", data=_conv_body())
    dj, fp = _both(fast, django, owner, f"{CONV}/c-1", method="delete")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content == b'{"deleted": true}'


def test_conversation_user_isolation(fast, django, owner):
    other = APIClient()  # a second user's convo must not leak
    from django.contrib.auth import get_user_model

    u2 = get_user_model().objects.create_user(
        username="as-other", email="as-o@e.com", password="pw"
    )
    other.credentials(HTTP_AUTHORIZATION=f"Bearer {_token(u2)}")
    other.put(f"{CONV}/c-secret", _conv_body(title="secret"), format="json")
    dj, fp = _both(fast, django, owner, CONV)
    assert dj.content == fp.content == b"[]"


# --- tool invokes reading seeded rows: JSON parity -------------------------


def test_get_active_alerts_json_parity(fast, django, owner):
    zone = _make_zone(owner, "North field")
    _make_alert(owner, zone=zone)
    dj, fp = _both(
        fast,
        django,
        owner,
        f"{TOOLS}/get_active_alerts",
        method="post",
        data={"params": {}},
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.json() == fp.json()
    alert = fp.json()["data"]["alerts"][0]
    assert alert["name"] == "Low soil moisture"
    assert alert["zone"] == "North field"
    assert alert["threshold"] == 20.0
    assert alert["severity"] == "ok"


def test_get_farm_status_json_parity_no_readings(fast, django, owner):
    dj, fp = _both(
        fast,
        django,
        owner,
        f"{TOOLS}/get_farm_status",
        method="post",
        data={"params": {}},
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.json() == fp.json()


def test_list_recent_notifications_json_parity(fast, django, owner):
    from apps.alerts.models import Notification

    Notification.objects.create(
        user=owner,
        yesterday_temperature=20,
        today_temperature=22,
        yesterday_humidity=50,
        today_humidity=55,
        ET0=4.2,
        soil_humidity=33,
        soil_temperature=18,
        soil_ph=6.8,
        perfect_irrigation_period="06:00 - 07:00",
        last_irrigation_date=datetime.date(2026, 6, 1),
        last_start_irrigation_hour=datetime.time(6, 0),
        last_finish_irrigation_hour=datetime.time(7, 0),
        used_water_irrigation=1200,
        notification_date=datetime.datetime(
            2026, 6, 18, 9, 0, tzinfo=datetime.timezone.utc
        ),
    )
    dj, fp = _both(
        fast,
        django,
        owner,
        f"{TOOLS}/list_recent_notifications",
        method="post",
        data={"params": {}},
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.json() == fp.json()
    row = fp.json()["data"]["notifications"][0]
    assert "ET0 4.20 mm" in row["message"]
    assert row["type"] == "irrigation_summary"


# --- /chat rule-based (deterministic): byte-identical ----------------------


def test_chat_sitemap_byte_identical(fast, django, owner):
    dj, fp = _both(
        fast, django, owner, CHAT, method="post", data={"message": "/sitemap"}
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


def test_chat_alerts_json_parity(fast, django, owner):
    zone = _make_zone(owner, "North field")
    _make_alert(owner, zone=zone)
    dj, fp = _both(
        fast, django, owner, CHAT, method="post", data={"message": "/alerts"}
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.json() == fp.json()
    assert fp.json()["intent"] == "active_alerts"
    assert fp.json()["tool"] == "get_active_alerts"


def test_chat_smalltalk_byte_identical(fast, django, owner):
    dj, fp = _both(
        fast, django, owner, CHAT, method="post", data={"message": "hello there"}
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


def test_chat_commands_json_parity(fast, django, owner):
    dj, fp = _both(fast, django, owner, CHAT, method="post", data={"message": "/help"})
    assert dj.status_code == fp.status_code == 200
    assert dj.json() == fp.json()
    assert fp.json()["intent"] == "commands" and fp.json()["tool"] is None


# --- mutating tool: create_alert (ids differ; compare shape + row) ---------


def test_create_alert_writes_and_matches(fast, django, owner):
    body = {
        "params": {
            "name": "High VPD",
            "sensor_key": "vpd",
            "condition": ">",
            "condition_nbr": 1.5,
        }
    }
    dj, fp = _both(
        fast, django, owner, f"{TOOLS}/create_alert", method="post", data=body
    )
    assert dj.status_code == fp.status_code == 200
    dj_created = dj.json()["data"]["created"]
    fp_created = fp.json()["data"]["created"]
    # ids differ by construction; everything else must match.
    for c in (dj_created, fp_created):
        c.pop("id")
    assert dj_created == fp_created
    assert fp_created == {
        "name": "High VPD",
        "sensor_key": "vpd",
        "condition": ">",
        "condition_nbr": 1.5,
        "is_active": True,
    }


def test_create_alert_technician_blocked_byte_identical(fast, django, technician):
    body = {
        "params": {
            "name": "x",
            "sensor_key": "vpd",
            "condition": ">",
            "condition_nbr": 1.5,
        }
    }
    dj, fp = _both(
        fast, django, technician, f"{TOOLS}/create_alert", method="post", data=body
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


# --- /chat LLM path: mock _post on BOTH sides, assert envelope matches ------


def test_chat_llm_envelope_parity(fast, django, owner, monkeypatch):
    """With an AI key set, /chat runs the tool-calling LLM orchestrator. Mock the
    HTTP call identically on both sides: the model 'chooses' get_farm_status,
    then replies with fixed text. The reply text is the canned mock (NOT
    byte-matchable against a live model in prod) — here it lets us prove the
    envelope + tool-routing + tool data match across surfaces."""
    monkeypatch.setattr(get_settings(), "ai_api_key", "test-key")
    monkeypatch.setattr(dj_settings, "AI_API_KEY", "test-key", raising=False)

    tool_call_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "get_farm_status", "arguments": "{}"},
                        }
                    ],
                }
            }
        ]
    }
    final_resp = {
        "choices": [
            {"message": {"role": "assistant", "content": "Your soil moisture is low."}}
        ]
    }
    responses = [tool_call_resp, final_resp]

    def fake_post(payload):
        return responses[0] if payload.get("tools") else responses[1]

    monkeypatch.setattr("fastapp.assistant.llm._post", fake_post)
    monkeypatch.setattr("apps.assistant.llm._post", fake_post)

    dj, fp = _both(
        fast, django, owner, CHAT, method="post", data={"message": "how is my farm?"}
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.json() == fp.json()
    assert fp.json()["intent"] == "llm"
    assert fp.json()["tool"] == "get_farm_status"
    assert fp.json()["reply"] == "Your soil moisture is low."
