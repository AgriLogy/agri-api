"""Tests for the assistant tool layer + orchestrator + HTTP surface."""

from __future__ import annotations

from unittest import mock

import pytest

from apps.assistant.orchestrator import (
    RuleBasedOrchestrator,
    get_orchestrator,
)

TOOLS_URL = "/assistant/tools"
CHAT_URL = "/assistant/chat"


def _fake_run_tool(name, params):
    """Canned tool runner so orchestrator tests stay pure (no DB)."""
    return {"ran": name, "params": params}


# ── orchestrator (pure, no DB) ───────────────────────────────────────────────
class TestOrchestrator:
    def setup_method(self):
        self.orch = RuleBasedOrchestrator()

    @pytest.mark.parametrize(
        "message,intent,tool",
        [
            ("/sitemap", "sitemap", "get_sitemap"),
            ("/help", "commands", None),
            ("/alerts", "active_alerts", "get_active_alerts"),
            ("/status", "farm_status", "get_farm_status"),
            ("/weather", "weather", "get_weather"),
            ("/clear", "clear", None),
            ("show me the site map", "sitemap", "get_sitemap"),
            ("what are my alertes", "active_alerts", "get_active_alerts"),
            ("météo", "weather", "get_weather"),
        ],
    )
    def test_routes(self, message, intent, tool):
        d = self.orch.decide(message)
        assert d.intent == intent
        assert d.tool == tool
        assert d.reply_key.startswith("misc.chatbot.")

    def test_fallback_is_smalltalk(self):
        d = self.orch.decide("tell me a joke")
        assert d.intent == "smalltalk"
        assert d.tool is None


# ── HTTP surface ─────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestAssistantApi:
    def test_tools_catalog(self, assistant_client):
        r = assistant_client.get(TOOLS_URL)
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["tools"]}
        assert {
            "get_sitemap",
            "get_active_alerts",
            "get_farm_status",
            "get_weather",
        } <= names

    def test_invoke_farm_status(self, assistant_client):
        r = assistant_client.post(
            f"{TOOLS_URL}/get_farm_status", {"params": {}}, format="json"
        )
        assert r.status_code == 200
        metrics = r.json()["data"]["metrics"]
        keys = {m["key"] for m in metrics}
        assert "soilMoisture" in keys and "vpd" in keys
        # No seeded readings → value None, status unknown (but well-formed).
        for m in metrics:
            assert "label" in m and "unit" in m and "status" in m

    def test_invoke_unknown_tool_404(self, assistant_client):
        r = assistant_client.post(f"{TOOLS_URL}/nope", {"params": {}}, format="json")
        assert r.status_code == 404

    def test_chat_sitemap(self, assistant_client):
        r = assistant_client.post(CHAT_URL, {"message": "/sitemap"}, format="json")
        assert r.status_code == 200
        body = r.json()
        assert body["intent"] == "sitemap"
        assert body["tool"] == "get_sitemap"
        assert len(body["data"]["routes"]) > 0

    def test_chat_alerts_returns_user_alerts(self, assistant_client, assistant_user):
        from apps.alerts.models import Alert

        Alert.objects.create(
            user=assistant_user,
            name="Low soil moisture",
            condition="<",
            condition_nbr=20,
            sensor_key="soilMoisture",
            is_active=True,
        )
        r = assistant_client.post(CHAT_URL, {"message": "/alerts"}, format="json")
        assert r.status_code == 200
        body = r.json()
        assert body["intent"] == "active_alerts"
        names = [a["name"] for a in body["data"]["alerts"]]
        assert "Low soil moisture" in names

    def test_chat_commands_returns_catalog(self, assistant_client):
        r = assistant_client.post(CHAT_URL, {"message": "/help"}, format="json")
        body = r.json()
        assert body["intent"] == "commands"
        assert body["tool"] is None
        assert len(body["data"]["commands"]) >= 4

    def test_chat_smalltalk_has_no_tool(self, assistant_client):
        r = assistant_client.post(CHAT_URL, {"message": "hello there"}, format="json")
        body = r.json()
        assert body["intent"] == "smalltalk"
        assert body["tool"] is None
        assert body["data"] is None

    def test_requires_auth(self):
        from rest_framework.test import APIClient

        r = APIClient().get(TOOLS_URL)
        assert r.status_code == 401


# ── orchestrator factory + LLM orchestrator (pure, no DB) ────────────────────
class TestLLMOrchestrator:
    def test_factory_defaults_to_rule_based_without_key(self, settings):
        settings.AI_API_KEY = ""
        assert isinstance(get_orchestrator(), RuleBasedOrchestrator)

    def test_factory_uses_llm_when_key_set(self, settings):
        from apps.assistant.llm import LLMOrchestrator

        settings.AI_API_KEY = "test-key"
        assert isinstance(get_orchestrator(), LLMOrchestrator)

    def test_tool_schemas_are_openai_shaped(self):
        from apps.assistant.llm import _tool_schemas

        schemas = _tool_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert {"get_sitemap", "get_farm_status"} <= names
        for s in schemas:
            assert s["type"] == "function"
            assert "parameters" in s["function"]
        farm = next(s for s in schemas if s["function"]["name"] == "get_farm_status")
        assert "zone_id" in farm["function"]["parameters"]["properties"]

    def test_falls_back_to_rule_based_on_api_error(self):
        from apps.assistant.llm import LLMOrchestrator

        orch = LLMOrchestrator(fallback=RuleBasedOrchestrator())
        with mock.patch("apps.assistant.llm._post", side_effect=RuntimeError("boom")):
            res = orch.respond("/sitemap", run_tool=_fake_run_tool)
        # Fell back: rule-based mapped /sitemap → get_sitemap with a reply_key.
        assert res.intent == "sitemap"
        assert res.tool == "get_sitemap"
        assert res.reply_key == "misc.chatbot.sitemap.intro"

    def test_runs_tool_and_returns_model_reply(self):
        from apps.assistant.llm import LLMOrchestrator

        orch = LLMOrchestrator(fallback=RuleBasedOrchestrator())
        tool_call_resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "get_farm_status",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                }
            ]
        }
        final_resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Your soil moisture is low.",
                    }
                }
            ]
        }
        with mock.patch(
            "apps.assistant.llm._post", side_effect=[tool_call_resp, final_resp]
        ):
            res = orch.respond("how is my farm?", run_tool=_fake_run_tool)
        assert res.intent == "llm"
        assert res.tool == "get_farm_status"
        assert res.data == {"ran": "get_farm_status", "params": {}}
        assert res.reply == "Your soil moisture is low."

    def test_direct_answer_without_tool(self):
        from apps.assistant.llm import LLMOrchestrator

        orch = LLMOrchestrator(fallback=RuleBasedOrchestrator())
        resp = {"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]}
        with mock.patch("apps.assistant.llm._post", return_value=resp):
            res = orch.respond("hi", run_tool=_fake_run_tool)
        assert res.intent == "llm"
        assert res.tool is None
        assert res.reply == "Hello!"
