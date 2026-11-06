"""Provider-agnostic assistant engine + back-office LLM settings (#461).

Covers, without any real LLM call:

* admin config: save / read / key never returned / encrypted at rest /
  validation / admin-only / hidden from the generic settings endpoints;
* /assistant/chat on the LLM path through a fake provider: history, selected
  zone + language reach the model, confirm-before-write (a mutating tool is
  proposed, never executed), sources with ``as_of``, fallback on provider
  failure, commands still rule-based;
* the two real providers' wire handling with fake transports (Anthropic SDK
  client stub; httpx MockTransport for OpenAI-compatible / Ollama);
* tool metrics carry each reading's ``as_of``.

Dual-ORM: Postgres + committed rows.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from django.conf import settings as dj_settings
from django.db import connection
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.assistant import config as llm_config
from fastapp.assistant import providers as providers_mod
from fastapp.assistant.providers import (
    ChatTurn,
    LLMResult,
    ProviderConfig,
    ProviderError,
    ToolCallRecord,
    ToolSpec,
)
from fastapp.assistant.providers.anthropic_provider import AnthropicProvider
from fastapp.assistant.providers.openai_compat import OpenAICompatProvider
from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM: fastapp reads the test DB Django writes (needs Postgres)",
)
pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

CONFIG = "/admin/assistant/config"
CHAT = "/assistant/chat"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)
    monkeypatch.setattr(get_settings(), "ai_api_key", "")


@pytest.fixture(autouse=True)
def _clean_llm_setting():
    """analytics_systemsetting is unmanaged (not flushed between tests): a left
    over config would switch other suites' /chat onto the LLM path."""
    yield
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM analytics_systemsetting WHERE key = %s",
            [llm_config.SETTING_KEY],
        )


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(django_user_model, set_access_level):
    user = django_user_model.objects.create_user(
        username="ai-admin", email="ai-admin@example.com", password="irrelevant-3921"
    )
    set_access_level(user, "admin")
    return user


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="ai-farmer", email="ai-farmer@example.com", password="irrelevant-3921"
    )


@pytest.fixture
def zone(owner):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=owner,
        name="Parcelle Olivier",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


def _save(fast, admin, **body):
    payload = {"enabled": True, "provider": "anthropic", "model": "claude-haiku-5-5"}
    payload.update(body)
    return fast.put(CONFIG, json=payload, headers=_auth(admin))


class FakeProvider:
    """Scripted provider: optionally calls tools, records what it was given."""

    def __init__(self, calls=(), text="Réponse.", error: ProviderError | None = None):
        self.calls = list(calls)
        self.text = text
        self.error = error
        self.seen: dict = {}

    def complete(self, *, system, context, history, message, tools, execute):
        self.seen = {
            "system": system,
            "context": context,
            "history": history,
            "message": message,
            "tools": [t.name for t in tools],
        }
        if self.error:
            raise self.error
        records = []
        for name, tool_input in self.calls:
            records.append(ToolCallRecord(name, tool_input, execute(name, tool_input)))
        return LLMResult(text=self.text, tool_calls=records, model="fake-model")


@pytest.fixture
def fake(monkeypatch, fast, admin):
    """Configure the assistant and route build_provider to a FakeProvider."""
    holder = {"provider": FakeProvider()}
    assert _save(fast, admin, api_key="sk-ant-test-1234").status_code == 200
    monkeypatch.setattr(providers_mod, "build_provider", lambda cfg: holder["provider"])
    return holder


# ===========================================================================
# Back-office config
# ===========================================================================
def test_config_round_trip_never_returns_the_key(fast, admin):
    res = _save(fast, admin, api_key="sk-ant-secret-ABCD", effort="medium")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "settings"
    assert body["active"] is True
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-haiku-5-5"
    assert body["effort"] == "medium"
    assert body["has_api_key"] is True
    assert body["api_key_hint"] == "ABCD"
    assert "sk-ant-secret" not in res.text
    assert set(body["providers"]) == {"anthropic", "openai", "groq", "ollama"}

    got = fast.get(CONFIG, headers=_auth(admin))
    assert got.status_code == 200
    assert "sk-ant-secret" not in got.text

    # Encrypted at rest: the stored row holds ciphertext, not the key.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT value::text FROM analytics_systemsetting WHERE key = %s",
            [llm_config.SETTING_KEY],
        )
        raw = cursor.fetchone()[0]
    assert "sk-ant-secret" not in raw
    stored = json.loads(raw)
    assert llm_config.decrypt_secret(stored["api_key_enc"]) == "sk-ant-secret-ABCD"


def test_omitted_key_is_kept_and_empty_key_removes_it(fast, admin):
    _save(fast, admin, api_key="sk-ant-first-1111")
    kept = _save(fast, admin, model="claude-sonnet-5-5")  # no api_key field
    assert kept.json()["api_key_hint"] == "1111"
    assert kept.json()["model"] == "claude-sonnet-5-5"

    removed = fast.put(
        CONFIG,
        json={"enabled": False, "provider": "anthropic", "api_key": ""},
        headers=_auth(admin),
    )
    assert removed.status_code == 200
    assert removed.json()["has_api_key"] is False
    assert removed.json()["active"] is False


def test_config_validation(fast, admin):
    h = _auth(admin)
    assert fast.put(CONFIG, json={"provider": "nope"}, headers=h).status_code == 400
    no_key = fast.put(
        CONFIG, json={"enabled": True, "provider": "anthropic"}, headers=h
    )
    assert no_key.json() == {"detail": "an API key is required for this provider."}
    bad_effort = _save(fast, admin, api_key="k-1234", effort="max")
    assert bad_effort.status_code == 400
    bad_url = fast.put(
        CONFIG,
        json={
            "enabled": True,
            "provider": "openai",
            "model": "gpt-x",
            "base_url": "api.example.com",
            "api_key": "k",
        },
        headers=h,
    )
    assert bad_url.json() == {"detail": "base_url must start with http:// or https://."}
    # Ollama needs no key; the default base URL is filled in.
    ollama = fast.put(
        CONFIG,
        json={"enabled": True, "provider": "ollama", "model": "qwen2.5:7b"},
        headers=h,
    )
    assert ollama.status_code == 200, ollama.text
    assert ollama.json()["active"] is True
    assert ollama.json()["base_url"].endswith(":11434/v1")


def test_config_is_admin_only(fast, owner):
    assert fast.get(CONFIG, headers=_auth(owner)).status_code == 403
    assert (
        fast.put(CONFIG, json={"provider": "ollama"}, headers=_auth(owner)).status_code
        == 403
    )


def test_generic_settings_hide_and_refuse_the_reserved_key(fast, admin):
    _save(fast, admin, api_key="sk-ant-hidden-9999")
    admin_staff = _auth(admin)
    admin.is_staff = True
    admin.save()
    listed = fast.get("/admin/settings", headers=admin_staff)
    assert listed.status_code == 200
    assert llm_config.SETTING_KEY not in listed.text
    patched = fast.patch(
        "/admin/settings",
        json={"values": {llm_config.SETTING_KEY: {"enabled": False}}},
        headers=admin_staff,
    )
    assert patched.status_code == 400
    deleted = fast.delete(
        f"/admin/settings/{llm_config.SETTING_KEY}", headers=admin_staff
    )
    assert deleted.status_code == 400


def test_env_vars_are_the_fallback(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_api_key", "gsk-env-5678")
    from agri.core.database import session_scope

    with session_scope() as session:
        resolved = llm_config.resolve(session)
    assert resolved.source == "env"
    assert resolved.provider_config.provider == "openai"
    assert resolved.api_key_hint == "5678"


# ===========================================================================
# /assistant/chat on the LLM path
# ===========================================================================
def test_chat_passes_history_zone_and_language(fast, fake, owner, zone):
    res = fast.post(
        CHAT,
        json={
            "message": "kifach l7al dyal l2ard?",
            "zone_id": zone.id,
            "language": "ary",
            "history": [
                {"role": "assistant", "content": "dropped: must start with user"},
                {"role": "user", "content": "salam"},
                {"role": "assistant", "content": "wa3alaykum salam"},
                {"role": "system", "content": "ignored role"},
            ],
        },
        headers=_auth(owner),
    )
    assert res.status_code == 200, res.text
    assert res.json()["intent"] == "llm"
    assert res.json()["reply"] == "Réponse."
    seen = fake["provider"].seen
    assert seen["message"] == "kifach l7al dyal l2ard?"
    assert seen["history"] == [
        ChatTurn("user", "salam"),
        ChatTurn("assistant", "wa3alaykum salam"),
    ]
    assert "Parcelle Olivier" in seen["context"]
    assert f"zone_id {zone.id}" in seen["context"]
    assert "Moroccan Darija" in seen["context"]
    assert "Darija" in seen["system"]
    assert "create_alert" in seen["tools"]


def test_foreign_zone_is_not_given_to_the_model(fast, fake, owner, django_user_model):
    from apps.irrigation.models import Zone

    other = django_user_model.objects.create_user(
        username="ai-other", email="ai-other@example.com", password="irrelevant-3921"
    )
    foreign = Zone.objects.create(
        user=other,
        name="Not yours",
        space=1.0,
        critical_moisture_threshold=1.0,
        pomp_flow_rate=1.0,
    )
    fast.post(
        CHAT, json={"message": "status?", "zone_id": foreign.id}, headers=_auth(owner)
    )
    assert "Not yours" not in fake["provider"].seen["context"]


def test_mutating_tool_is_proposed_never_executed(fast, fake, owner, zone):
    from apps.alerts.models import Alert

    fake["provider"] = FakeProvider(
        calls=[
            (
                "create_alert",
                {
                    "name": "Sol sec",
                    "sensor_key": "soilMoistureMedium",
                    "condition": "<",
                    "condition_nbr": 20,
                },
            )
        ],
        text="Je peux créer cette alerte : confirmez-la.",
    )
    res = fast.post(
        CHAT,
        json={"message": "dir lia alerte ila nqsat rtoba 3la 20", "zone_id": zone.id},
        headers=_auth(owner),
    )
    assert res.status_code == 200, res.text
    pending = res.json()["pending_action"]
    assert pending["tool"] == "create_alert"
    assert pending["params"]["condition_nbr"] == 20
    assert pending["params"]["zone_id"] == zone.id  # selected zone filled in
    assert Alert.objects.filter(user=owner).count() == 0  # NOT executed
    # Confirming is the existing authenticated tool route, as the farmer.
    done = fast.post(
        f"/assistant/tools/{pending['tool']}",
        json={"params": pending["params"]},
        headers=_auth(owner),
    )
    assert done.status_code == 200
    assert done.json()["data"]["created"]["sensor_key"] == "soilMoistureMedium"
    assert Alert.objects.filter(user=owner, zone=zone).count() == 1


def test_sources_carry_reading_times(fast, fake, owner, zone):
    from apps.sensors.models import SoilMoistureMedium

    taken = dt.datetime(2026, 6, 22, 8, 26, tzinfo=dt.timezone.utc)
    SoilMoistureMedium.objects.create(
        user=owner, zone=zone, value=27.0, timestamp=taken
    )
    fake["provider"] = FakeProvider(calls=[("get_soil_status", {"zone_id": zone.id})])
    res = fast.post(
        CHAT, json={"message": "soil?", "zone_id": zone.id}, headers=_auth(owner)
    )
    body = res.json()
    assert body["tool"] == "get_soil_status"
    (source,) = body["sources"]
    assert source["tool"] == "get_soil_status"
    item = next(i for i in source["items"] if i["value"] == 27.0)
    assert item["as_of"].startswith("2026-06-22T08:26")
    assert source["as_of"].startswith("2026-06-22T08:26")


def test_provider_failure_falls_back_to_rule_based(fast, fake, owner):
    fake["provider"] = FakeProvider(error=ProviderError("rate_limit", "slow down"))
    res = fast.post(CHAT, json={"message": "mes alertes stp"}, headers=_auth(owner))
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "active_alerts"  # rule-based matched "mes alertes"
    assert body["fallback_reason"] == "rate_limit"


def test_commands_stay_rule_based_with_an_llm_configured(fast, fake, owner):
    res = fast.post(CHAT, json={"message": "/alerts"}, headers=_auth(owner))
    assert res.json()["intent"] == "active_alerts"
    assert fake["provider"].seen == {}  # the LLM was never called
    assert set(res.json()) == {"intent", "reply_key", "reply", "tool", "data"}


def test_no_config_means_rule_based_envelope(fast, owner):
    res = fast.post(CHAT, json={"message": "how is my farm?"}, headers=_auth(owner))
    assert set(res.json()) == {"intent", "reply_key", "reply", "tool", "data"}


def test_tool_metrics_carry_as_of(fast, owner, zone):
    from apps.sensors.models import SoilMoistureMedium

    taken = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)
    SoilMoistureMedium.objects.create(
        user=owner, zone=zone, value=30.0, timestamp=taken
    )
    res = fast.post(
        "/assistant/tools/get_soil_status",
        json={"params": {"zone_id": zone.id}},
        headers=_auth(owner),
    )
    metric = next(m for m in res.json()["data"]["metrics"] if m["value"] == 30.0)
    assert metric["as_of"].startswith("2026-06-01T12:00")


def test_admin_test_endpoint_reports_provider_errors(fast, admin, monkeypatch):
    class Failing:
        def complete(self, **kw):
            raise ProviderError("auth", "The API key was rejected.")

    monkeypatch.setattr(
        "fastapp.routers.admin_assistant.build_provider", lambda cfg: Failing()
    )
    res = fast.post(
        "/admin/assistant/test",
        json={"provider": "openai", "model": "gpt-x", "api_key": "bad"},
        headers=_auth(admin),
    )
    assert res.status_code == 200
    assert res.json()["ok"] is False
    assert res.json()["error_kind"] == "auth"


# ===========================================================================
# Provider wire handling (no network)
# ===========================================================================
def _block(**kw):
    return SimpleNamespace(**kw)


class _FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _anthropic(responses, model="claude-haiku-5-5"):
    msgs = _FakeMessages(responses)
    client = SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs))
    provider = AnthropicProvider(
        ProviderConfig(provider="anthropic", model=model, api_key="k"), client=client
    )
    return provider, msgs


def _msg(stop_reason, content):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        model="claude-haiku-5-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


_SPECS = [ToolSpec("get_weather", "Weather.", {"type": "object", "properties": {}})]


def test_anthropic_tool_loop_shapes():
    provider, msgs = _anthropic(
        [
            _msg(
                "tool_use",
                [
                    _block(type="tool_use", id="tu_1", name="get_weather", input={}),
                    _block(
                        type="tool_use", id="tu_2", name="get_weather", input={"x": 1}
                    ),
                ],
            ),
            _msg("end_turn", [_block(type="text", text="Il fait **26 °C**.")]),
        ]
    )
    outputs = iter([{"metrics": []}, {"error": "boom"}])
    result = provider.complete(
        system="S",
        context="C",
        history=[ChatTurn("user", "hi"), ChatTurn("assistant", "yo")],
        message="météo?",
        tools=_SPECS,
        execute=lambda n, i: next(outputs),
    )
    assert result.text == "Il fait **26 °C**."
    assert [r.name for r in result.tool_calls] == ["get_weather", "get_weather"]
    first = msgs.requests[0]
    assert "temperature" not in first
    assert first["output_config"] == {"effort": "low"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["system"][1]["text"] == "C"
    assert first["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    # Both tool results go back in ONE user message, errors flagged.
    results = msgs.requests[1]["messages"][-1]
    assert results["role"] == "user"
    assert [r["tool_use_id"] for r in results["content"]] == ["tu_1", "tu_2"]
    assert [r["is_error"] for r in results["content"]] == [False, True]
    assert result.usage["input_tokens"] == 20


def test_anthropic_uses_server_fallback_on_supported_models():
    provider, msgs = _anthropic(
        [_msg("end_turn", [_block(type="text", text="OK")])], model="claude-opus-5-5"
    )
    provider.complete(
        system="S", context="C", history=[], message="hi", tools=[], execute=dict
    )
    assert msgs.requests[0]["fallbacks"] == "default"
    assert msgs.requests[0]["betas"] == ["server-side-fallback-2026-07-01"]


def test_anthropic_refusal_and_error_mapping():
    provider, _ = _anthropic([_msg("refusal", [])])
    with pytest.raises(ProviderError) as exc:
        provider.complete(
            system="S", context="C", history=[], message="x", tools=[], execute=dict
        )
    assert exc.value.kind == "refusal"

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    rate = anthropic.RateLimitError(
        "slow", response=httpx.Response(429, request=req), body=None
    )
    provider, _ = _anthropic([rate])
    with pytest.raises(ProviderError) as exc:
        provider.complete(
            system="S", context="C", history=[], message="x", tools=[], execute=dict
        )
    assert exc.value.kind == "rate_limit"


def _openai(handler, provider="openai", api_key="k"):
    cfg = ProviderConfig(
        provider=provider, model="m", api_key=api_key, base_url="https://llm.test/v1"
    )
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    http = httpx.Client(
        base_url=cfg.base_url, headers=headers, transport=httpx.MockTransport(handler)
    )
    return OpenAICompatProvider(cfg, http=http)


def test_openai_compat_tool_loop():
    seen: list[dict] = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "model": "m",
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "Sec."}}]})

    provider = _openai(handler)
    result = provider.complete(
        system="S",
        context="C",
        history=[],
        message="?",
        tools=_SPECS,
        execute=lambda n, i: {"metrics": []},
    )
    assert result.text == "Sec."
    assert [r.name for r in result.tool_calls] == ["get_weather"]
    assert seen[0]["messages"][0] == {"role": "system", "content": "S"}
    tool_msg = seen[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    # Groq returns 400 on messages[].name, so no message may carry one.
    assert all("name" not in m for m in seen[1]["messages"])


@pytest.mark.parametrize(
    "status,kind",
    [(401, "auth"), (404, "bad_request"), (429, "rate_limit"), (503, "unavailable")],
)
def test_openai_compat_error_mapping(status, kind):
    provider = _openai(lambda request: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(ProviderError) as exc:
        provider.complete(
            system="S", context="C", history=[], message="?", tools=[], execute=dict
        )
    assert exc.value.kind == kind


def test_ollama_sends_no_auth_header():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = _openai(handler, provider="ollama", api_key="")
    provider.complete(
        system="S", context="C", history=[], message="?", tools=[], execute=dict
    )
    assert seen["auth"] is None


def test_groq_preset_uses_the_openai_compat_client():
    meta = providers_mod.PROVIDER_CATALOG["groq"]
    assert meta["default_base_url"] == "https://api.groq.com/openai/v1"
    assert meta["default_model"] in meta["suggested_models"]
    cfg = ProviderConfig(
        provider="groq",
        model=meta["default_model"],
        api_key="gsk-test",
        base_url=meta["default_base_url"],
    )
    assert isinstance(providers_mod.build_provider(cfg), OpenAICompatProvider)
