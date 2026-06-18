"""Tests for the assistant tool layer + orchestrator + HTTP surface."""

from __future__ import annotations

import pytest

from apps.assistant.orchestrator import RuleBasedOrchestrator

TOOLS_URL = "/assistant/tools"
CHAT_URL = "/assistant/chat"


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
