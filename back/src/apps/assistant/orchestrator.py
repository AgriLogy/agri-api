"""Orchestrator — understands the user's message and picks the right tool.

The router calls `get_orchestrator().decide(message)` to map free text (slash
commands or natural language, fr/en/ar) onto an intent + a tool + a localized
reply key. It deliberately returns only a *decision*; the router invokes the
chosen tool. That keeps the brain (routing) and the hands (data access)
separate, so the rule-based implementation here can be swapped for an
LLM tool-caller (Claude tool-use over `tools.registry.catalog()`) without
touching the tools or the HTTP surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# Injected by the router: invoke a registered tool by name and return its data.
RunTool = Callable[[str, dict], dict]


@dataclass(frozen=True)
class Decision:
    """What the orchestrator decided to do with a message."""

    intent: str
    reply_key: str
    tool: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantResponse:
    """The full result the router serializes to the client.

    `reply_key` is an i18n key (rule-based, localized on the frontend); `reply`
    is free text (LLM-generated). Exactly one is typically set.
    """

    intent: str
    tool: str | None = None
    data: dict | None = None
    reply_key: str | None = None
    reply: str | None = None


class Orchestrator(Protocol):
    def respond(
        self, message: str, *, run_tool: RunTool, context: str | None = None
    ) -> AssistantResponse: ...


# ── rule-based v1 ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _IntentRule:
    intent: str
    reply_key: str
    triggers: list[str]
    tool: str | None = None


_RULES: list[_IntentRule] = [
    _IntentRule(
        "sitemap",
        "misc.chatbot.sitemap.intro",
        [
            "/sitemap",
            "/map",
            "/pages",
            "sitemap",
            "site map",
            "plan du site",
            "navigation",
            "خريطة الموقع",
        ],
        tool="get_sitemap",
    ),
    _IntentRule(
        "commands",
        "misc.chatbot.commandsCard.intro",
        [
            "/help",
            "/commands",
            "/aide",
            "help",
            "aide",
            "commandes",
            "مساعدة",
            "الأوامر",
        ],
    ),
    _IntentRule(
        "active_alerts",
        "misc.chatbot.alertsCard.intro",
        [
            "/alerts",
            "/alertes",
            "alerts",
            "alertes",
            "my alerts",
            "mes alertes",
            "تنبيهات",
        ],
        tool="get_active_alerts",
    ),
    _IntentRule(
        "farm_status",
        "misc.chatbot.statusCard.intro",
        [
            "/status",
            "/farm",
            "/etat",
            "farm status",
            "état de la ferme",
            "etat de la ferme",
            "حالة المزرعة",
        ],
        tool="get_farm_status",
    ),
    _IntentRule(
        "weather",
        "misc.chatbot.weatherCard.intro",
        ["/weather", "/meteo", "/météo", "weather", "météo", "meteo", "طقس"],
        tool="get_weather",
    ),
    _IntentRule(
        "clear",
        "misc.chatbot.cleared",
        ["/clear", "/clr", "/effacer", "clear chat", "effacer la conversation"],
    ),
]

_FALLBACK = Decision(intent="smalltalk", reply_key="misc.chatbot.mock.generic")


class RuleBasedOrchestrator:
    """Deterministic keyword/slash matcher. Same vocabulary as the frontend."""

    def decide(self, message: str, *, context: str | None = None) -> Decision:
        text = (message or "").strip().lower()
        # 1) exact / slash-prefix
        for rule in _RULES:
            for trig in rule.triggers:
                if text == trig or (trig.startswith("/") and text.startswith(trig)):
                    return Decision(rule.intent, rule.reply_key, rule.tool)
        # 2) natural-language contains (guard tiny tokens)
        for rule in _RULES:
            for trig in rule.triggers:
                if not trig.startswith("/") and len(trig) >= 4 and trig in text:
                    return Decision(rule.intent, rule.reply_key, rule.tool)
        return _FALLBACK

    def respond(
        self, message: str, *, run_tool: RunTool, context: str | None = None
    ) -> AssistantResponse:
        d = self.decide(message, context=context)
        data: dict | None = None
        if d.tool:
            data = run_tool(d.tool, d.params)
        elif d.intent == "commands":
            from .tools import registry

            data = {"commands": registry.catalog()}
        return AssistantResponse(
            intent=d.intent, tool=d.tool, data=data, reply_key=d.reply_key
        )


def get_orchestrator() -> Orchestrator:
    """Factory — LLM orchestrator when AI_API_KEY is configured (with the
    rule-based one as its fallback), otherwise the rule-based orchestrator."""
    from django.conf import settings

    if getattr(settings, "AI_API_KEY", ""):
        from .llm import LLMOrchestrator

        return LLMOrchestrator(fallback=RuleBasedOrchestrator())
    return RuleBasedOrchestrator()
