"""LLM orchestrator — a provider-agnostic, tool-calling assistant brain.

Talks to any OpenAI-compatible chat-completions API (Groq, OpenRouter, Together,
OpenAI, …) configured via AI_API_BASE_URL / AI_MODEL / AI_API_KEY. It feeds the
LLM the live tool catalog (`registry.catalog()`), lets it choose a tool, runs
that tool through the injected `run_tool` (so data access stays behind the same
registry the HTTP routes use), feeds the result back, and returns the model's
natural-language answer plus the structured tool data for the card.

Uses the stdlib (urllib) — no new dependency, matching the Resend/Twilio
backends. Any failure (no key, timeout, rate-limit, bad response) degrades
gracefully to the rule-based orchestrator.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

from .orchestrator import AssistantResponse, Orchestrator, RunTool
from .tools import registry

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the Agrilogy assistant, embedded in a precision-farming dashboard. "
    "Help the user understand and navigate their farm data: active alerts, the "
    "latest sensor readings (soil moisture, temperature, VPD, ET0, humidity), "
    "weather, and the app's pages. ALWAYS use the provided tools to fetch live "
    "data — never invent numbers. Be concise and practical, and reply in the "
    "user's language (French, English, or Arabic). If a request is unrelated to "
    "Agrilogy or farming, politely decline.\n"
    "Never mention tool or function names, and never write function-call syntax, "
    "tags, or pseudo-code such as <function=...> in your answer — just give the "
    "answer in natural prose. Format replies with simple Markdown: short "
    "paragraphs, **bold** for key values, and bullet lists where they help."
)


def _tool_schemas() -> list[dict]:
    """Convert the tool catalog into OpenAI `tools` (function) schemas."""
    schemas = []
    for tool in registry.catalog():
        properties: dict[str, dict] = {}
        required: list[str] = []
        for pname, pinfo in (tool.get("params") or {}).items():
            properties[pname] = {
                "type": pinfo.get("type", "string"),
                "description": pinfo.get("description", ""),
            }
            if pinfo.get("required"):
                required.append(pname)
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return schemas


def _post(payload: dict) -> dict:
    url = f"{settings.AI_API_BASE_URL.rstrip('/')}/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.AI_API_KEY}",
            # Some providers' edge/WAF (e.g. Groq) reject the default
            # "Python-urllib/x.y" agent with 403 — send an explicit one.
            "User-Agent": "agri-api-assistant/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=settings.AI_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


class LLMOrchestrator:
    """Tool-calling orchestrator; falls back to `fallback` on any failure."""

    def __init__(self, fallback: Orchestrator) -> None:
        self.fallback = fallback

    def respond(
        self, message: str, *, run_tool: RunTool, context: str | None = None
    ) -> AssistantResponse:
        try:
            return self._respond(message, run_tool=run_tool, context=context)
        except Exception:
            logger.exception("LLM orchestrator failed; falling back to rule-based")
            return self.fallback.respond(message, run_tool=run_tool, context=context)

    def _respond(
        self, message: str, *, run_tool: RunTool, context: str | None = None
    ) -> AssistantResponse:
        system = SYSTEM_PROMPT
        if context:
            system += f"\nThe user is currently on the '{context}' page."
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]

        first = _post(
            {
                "model": settings.AI_MODEL,
                "messages": messages,
                "tools": _tool_schemas(),
                "tool_choice": "auto",
                "temperature": 0.2,
            }
        )
        choice = first["choices"][0]["message"]
        tool_calls = choice.get("tool_calls") or []

        if not tool_calls:
            return AssistantResponse(
                intent="llm", reply=(choice.get("content") or "").strip()
            )

        # The model wants data: run the tool(s), feed results back, ask again.
        messages.append(choice)
        used_tool: str | None = None
        used_data: dict | None = None
        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except (json.JSONDecodeError, KeyError, TypeError):
                args = {}
            try:
                data = run_tool(name, args)
            except Exception:
                logger.exception("tool %s failed inside LLM loop", name)
                data = {"error": f"tool {name} failed"}
            if used_tool is None:
                used_tool, used_data = name, data
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(data, default=str),
                }
            )

        second = _post(
            {
                "model": settings.AI_MODEL,
                "messages": messages,
                "temperature": 0.2,
            }
        )
        reply = (second["choices"][0]["message"].get("content") or "").strip()
        return AssistantResponse(
            intent="llm", tool=used_tool, data=used_data, reply=reply
        )
