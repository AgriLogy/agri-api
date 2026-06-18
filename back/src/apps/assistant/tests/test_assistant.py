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
            ("/zones", "zones", "list_zones"),
            ("/clear", "clear", None),
            ("show me the site map", "sitemap", "get_sitemap"),
            ("what are my alertes", "active_alerts", "get_active_alerts"),
            ("météo", "weather", "get_weather"),
            ("list zones please", "zones", "list_zones"),
            ("mes zones", "zones", "list_zones"),
            ("/soil", "soil", "get_soil_status"),
            ("how is my soil", "soil", "get_soil_status"),
            ("/plant", "plant", "get_plant_status"),
            ("leaf moisture", "plant", "get_plant_status"),
            ("/water", "water", "get_water_status"),
            ("water status", "water", "get_water_status"),
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
            "list_zones",
            "get_zone_detail",
            "get_soil_status",
            "get_plant_status",
            "get_water_status",
            "get_sensor_trend",
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


# ── zones tools ──────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestZonesTools:
    def _zone(self, user, name, **over):
        from apps.irrigation.models import Zone

        fields = {
            "user": user,
            "name": name,
            "space": 1000.0,
            "critical_moisture_threshold": 25.0,
        }
        fields.update(over)
        return Zone.objects.create(**fields)

    def test_list_zones_returns_callers_zones(self, assistant_client, assistant_user):
        self._zone(assistant_user, "North field")
        self._zone(assistant_user, "South field")
        r = assistant_client.post(
            f"{TOOLS_URL}/list_zones", {"params": {}}, format="json"
        )
        assert r.status_code == 200
        zones = r.json()["data"]["zones"]
        names = {z["name"] for z in zones}
        assert names == {"North field", "South field"}
        north = next(z for z in zones if z["name"] == "North field")
        assert north["area_m2"] == 1000.0
        assert north["critical_moisture"] == 25.0

    def test_list_zones_name_filter(self, assistant_client, assistant_user):
        self._zone(assistant_user, "North field")
        self._zone(assistant_user, "South orchard")
        r = assistant_client.post(
            f"{TOOLS_URL}/list_zones",
            {"params": {"zone_name": "orchard"}},
            format="json",
        )
        zones = r.json()["data"]["zones"]
        assert [z["name"] for z in zones] == ["South orchard"]

    def test_list_zones_user_isolation(self, assistant_client, assistant_user):
        from django.contrib.auth import get_user_model

        other = get_user_model().objects.create_user(
            username="other_zones", email="oz@e.com", password="pw"
        )
        self._zone(other, "Not mine")
        self._zone(assistant_user, "Mine")
        r = assistant_client.post(
            f"{TOOLS_URL}/list_zones", {"params": {}}, format="json"
        )
        names = {z["name"] for z in r.json()["data"]["zones"]}
        assert names == {"Mine"}

    def test_get_zone_detail_by_id(self, assistant_client, assistant_user):
        z = self._zone(assistant_user, "Plot A", pomp_flow_rate=42.0)
        r = assistant_client.post(
            f"{TOOLS_URL}/get_zone_detail",
            {"params": {"zone_id": z.id}},
            format="json",
        )
        detail = r.json()["data"]["zone"]
        assert detail["name"] == "Plot A"
        assert detail["pomp_flow_rate"] == 42.0

    def test_get_zone_detail_by_name(self, assistant_client, assistant_user):
        self._zone(assistant_user, "Plot B")
        r = assistant_client.post(
            f"{TOOLS_URL}/get_zone_detail",
            {"params": {"zone_name": "plot b"}},
            format="json",
        )
        assert r.json()["data"]["zone"]["name"] == "Plot B"

    def test_get_zone_detail_missing_returns_null(self, assistant_client):
        r = assistant_client.post(
            f"{TOOLS_URL}/get_zone_detail",
            {"params": {"zone_name": "nope"}},
            format="json",
        )
        assert r.status_code == 200
        assert r.json()["data"]["zone"] is None

    def test_chat_zones_returns_user_zones(self, assistant_client, assistant_user):
        self._zone(assistant_user, "Greenhouse")
        r = assistant_client.post(CHAT_URL, {"message": "/zones"}, format="json")
        body = r.json()
        assert body["intent"] == "zones"
        assert body["tool"] == "list_zones"
        names = [z["name"] for z in body["data"]["zones"]]
        assert "Greenhouse" in names


# ── soil / plant / water snapshot tools ──────────────────────────────────────
@pytest.mark.django_db
class TestSnapshotTools:
    def _zone(self, user, name="Z"):
        from apps.irrigation.models import Zone

        return Zone.objects.create(
            user=user, name=name, space=1000.0, critical_moisture_threshold=25.0
        )

    def _reading(self, model_path, user, value, zone):
        from importlib import import_module

        from django.utils import timezone

        module, name = model_path.rsplit(".", 1)
        Model = getattr(import_module(module), name)
        return Model.objects.create(
            user=user, zone=zone, value=value, timestamp=timezone.now()
        )

    @pytest.mark.parametrize(
        "tool,model_path,key",
        [
            ("get_soil_status", "apps.sensors.models.SoilMoistureMedium",
             "soilMoistureMedium"),
            ("get_plant_status", "apps.sensors.models.LeafMoistureSensor",
             "leafMoisture"),
            ("get_water_status", "apps.sensors.models.WaterFlowSensor",
             "waterFlow"),
        ],
    )
    def test_snapshot_returns_metric(
        self, assistant_client, assistant_user, tool, model_path, key
    ):
        self._reading(model_path, assistant_user, 42.0, self._zone(assistant_user))
        r = assistant_client.post(f"{TOOLS_URL}/{tool}", {"params": {}}, format="json")
        assert r.status_code == 200
        metrics = {m["key"]: m for m in r.json()["data"]["metrics"]}
        assert key in metrics
        assert metrics[key]["value"] == 42.0
        assert metrics[key]["label"] and metrics[key]["unit"]

    def test_soil_status_zone_filter(self, assistant_client, assistant_user):
        z = self._zone(assistant_user, "target")
        other_zone = self._zone(assistant_user, "other")
        self._reading(
            "apps.sensors.models.SoilMoistureMedium", assistant_user, 10.0, z
        )
        self._reading(
            "apps.sensors.models.SoilMoistureMedium", assistant_user, 90.0, other_zone
        )
        r = assistant_client.post(
            f"{TOOLS_URL}/get_soil_status",
            {"params": {"zone_id": z.id}},
            format="json",
        )
        m = {x["key"]: x for x in r.json()["data"]["metrics"]}
        assert m["soilMoistureMedium"]["value"] == 10.0

    def test_water_status_user_isolation(self, assistant_client, assistant_user):
        from django.contrib.auth import get_user_model

        other = get_user_model().objects.create_user(
            username="other_snap", email="os@e.com", password="pw"
        )
        self._reading(
            "apps.sensors.models.WaterFlowSensor", other, 5.0, self._zone(other)
        )
        r = assistant_client.post(
            f"{TOOLS_URL}/get_water_status", {"params": {}}, format="json"
        )
        m = {x["key"]: x for x in r.json()["data"]["metrics"]}
        # No reading for the caller → value None, but the metric is well-formed.
        assert m["waterFlow"]["value"] is None

    @pytest.mark.parametrize(
        "message,intent,tool",
        [
            ("/soil", "soil", "get_soil_status"),
            ("/plant", "plant", "get_plant_status"),
            ("/water", "water", "get_water_status"),
        ],
    )
    def test_chat_routes(self, assistant_client, message, intent, tool):
        r = assistant_client.post(CHAT_URL, {"message": message}, format="json")
        body = r.json()
        assert body["intent"] == intent
        assert body["tool"] == tool
        assert "metrics" in body["data"]


# ── sensor trend ─────────────────────────────────────────────────────────────
@pytest.mark.django_db
class TestSensorTrendTool:
    def _zone(self, user, name="Z"):
        from apps.irrigation.models import Zone

        return Zone.objects.create(
            user=user, name=name, space=1000.0, critical_moisture_threshold=25.0
        )

    def _reading(self, user, value, zone, minutes_ago):
        from datetime import timedelta

        from django.utils import timezone

        from apps.sensors.models import SoilMoistureMedium

        return SoilMoistureMedium.objects.create(
            user=user,
            zone=zone,
            value=value,
            timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def _trend(self, client, **params):
        return client.post(
            f"{TOOLS_URL}/get_sensor_trend", {"params": params}, format="json"
        )

    def test_stats_and_rising(self, assistant_client, assistant_user):
        z = self._zone(assistant_user)
        # oldest→newest 10,20,30 → rising, min10 max30 avg20 latest30 count3
        self._reading(assistant_user, 10.0, z, 180)
        self._reading(assistant_user, 20.0, z, 120)
        self._reading(assistant_user, 30.0, z, 60)
        d = self._trend(assistant_client, sensor_key="soilMoisture").json()["data"]
        assert d["count"] == 3
        assert (d["min"], d["max"], d["avg"]) == (10.0, 30.0, 20.0)
        assert d["latest"] == 30.0
        assert d["direction"] == "rising"
        assert d["key"] == "soilMoisture" and d["unit"]

    def test_falling(self, assistant_client, assistant_user):
        z = self._zone(assistant_user)
        self._reading(assistant_user, 40.0, z, 180)
        self._reading(assistant_user, 10.0, z, 30)
        d = self._trend(assistant_client, sensor_key="soilMoisture").json()["data"]
        assert d["direction"] == "falling"

    def test_flat_single_reading(self, assistant_client, assistant_user):
        z = self._zone(assistant_user)
        self._reading(assistant_user, 25.0, z, 30)
        d = self._trend(assistant_client, sensor_key="soilMoisture").json()["data"]
        assert d["direction"] == "flat"

    def test_window_excludes_old(self, assistant_client, assistant_user):
        z = self._zone(assistant_user)
        self._reading(assistant_user, 99.0, z, 60)  # inside 24h
        self._reading(assistant_user, 1.0, z, 60 * 48)  # 2 days ago, excluded
        d = self._trend(
            assistant_client, sensor_key="soilMoisture", hours=24
        ).json()["data"]
        assert d["count"] == 1 and d["min"] == 99.0

    def test_unknown_key_errors_no_500(self, assistant_client):
        r = self._trend(assistant_client, sensor_key="nope")
        assert r.status_code == 200
        assert "error" in r.json()["data"]

    def test_zone_filter(self, assistant_client, assistant_user):
        z = self._zone(assistant_user, "target")
        other = self._zone(assistant_user, "other")
        self._reading(assistant_user, 50.0, z, 30)
        self._reading(assistant_user, 5.0, other, 30)
        d = self._trend(
            assistant_client, sensor_key="soilMoisture", zone_id=z.id
        ).json()["data"]
        assert d["count"] == 1 and d["latest"] == 50.0

    def test_user_isolation(self, assistant_client, assistant_user):
        from django.contrib.auth import get_user_model

        other = get_user_model().objects.create_user(
            username="other_trend", email="ot@e.com", password="pw"
        )
        self._reading(other, 70.0, self._zone(other), 30)
        d = self._trend(assistant_client, sensor_key="soilMoisture").json()["data"]
        assert d["count"] == 0 and d["direction"] == "flat"

    def test_chat_trend_route(self, assistant_client, assistant_user):
        z = self._zone(assistant_user)
        self._reading(assistant_user, 33.0, z, 30)
        body = assistant_client.post(
            CHAT_URL, {"message": "/trend"}, format="json"
        ).json()
        assert body["intent"] == "trend"
        assert body["tool"] == "get_sensor_trend"
        assert body["data"]["key"] == "soilMoisture"
        assert body["data"]["direction"] in {"rising", "falling", "flat"}


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


# ── conversation history (server-side) ───────────────────────────────────────
CONV_URL = "/assistant/conversations"


@pytest.mark.django_db
class TestConversationHistory:
    def _conv(self, **over):
        body = {
            "title": "My chat",
            "messages": [{"id": "m1", "role": "user", "content": "hi"}],
            "created_at": "2026-06-18T10:00:00Z",
            "updated_at": "2026-06-18T10:00:00Z",
        }
        body.update(over)
        return body

    def test_list_empty(self, assistant_client):
        r = assistant_client.get(CONV_URL)
        assert r.status_code == 200
        assert r.json() == []

    def test_upsert_then_list(self, assistant_client):
        r = assistant_client.put(f"{CONV_URL}/c-1", self._conv(), format="json")
        assert r.status_code == 200
        assert r.json()["id"] == "c-1"
        lst = assistant_client.get(CONV_URL).json()
        assert len(lst) == 1 and lst[0]["title"] == "My chat"

    def test_upsert_replaces(self, assistant_client):
        assistant_client.put(f"{CONV_URL}/c-1", self._conv(), format="json")
        assistant_client.put(
            f"{CONV_URL}/c-1",
            self._conv(
                title="Renamed", messages=[{"id": "m1", "role": "user", "content": "x"}]
            ),
            format="json",
        )
        lst = assistant_client.get(CONV_URL).json()
        assert len(lst) == 1 and lst[0]["title"] == "Renamed"

    def test_delete(self, assistant_client):
        assistant_client.put(f"{CONV_URL}/c-1", self._conv(), format="json")
        r = assistant_client.delete(f"{CONV_URL}/c-1")
        assert r.status_code == 200
        assert assistant_client.get(CONV_URL).json() == []

    def test_user_isolation(self, assistant_client):
        # Another user's conversation must not be visible.
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        other = get_user_model().objects.create_user(
            username="other_assist", email="o@e.com", password="pw"
        )
        oc = APIClient()
        oc.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(other)}")
        oc.put(f"{CONV_URL}/c-other", self._conv(title="secret"), format="json")
        assert assistant_client.get(CONV_URL).json() == []

    def test_requires_auth(self):
        from rest_framework.test import APIClient

        assert APIClient().get(CONV_URL).status_code == 401
