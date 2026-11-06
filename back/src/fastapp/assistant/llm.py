"""LLM orchestrator: provider-agnostic, tool-calling assistant brain (#461).

The provider (Claude, OpenAI-compatible, Ollama) comes from the back-office
config (see ``fastapp.assistant.config``). This module owns what must not
depend on the provider:

* **confirm-before-write**: a ``mutating`` tool is never executed from chat.
  The model can only propose it; the response carries a ``pending_action`` the
  app renders as a confirmation button, which then calls the authenticated
  ``POST /assistant/tools/{name}`` as the farmer;
* **grounding**: the instruction prompt (``prompt.py``) plus per-request
  context (date, selected zone, language, page);
* **sources**: every tool result the answer used, with its readings' ``as_of``
  times, so the app can show what the advice is based on and how old it is;
* graceful degradation to the rule-based orchestrator on any provider error.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Any

from .orchestrator import AssistantResponse, Orchestrator, RunTool
from .prompt import SYSTEM_PROMPT, build_context
from .providers import (
    ChatTurn,
    LLMProvider,
    ProviderError,
    ToolCallRecord,
    ToolSpec,
)
from .tools import registry

logger = logging.getLogger(__name__)

# Bound what one chat turn sends upstream (cost + latency).
MAX_HISTORY_TURNS = 12
MAX_TURN_CHARS = 4000


@dataclass
class LLMAssistantResponse(AssistantResponse):
    sources: list[dict[str, Any]] = field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    model: str | None = None
    fallback_reason: str | None = None


def tool_specs() -> list[ToolSpec]:
    """The tool catalog as provider-neutral JSON-schema specs."""
    specs = []
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
        description = tool["description"]
        if tool.get("mutating"):
            description += (
                " This action is not executed directly: the farmer is shown a "
                "confirmation button."
            )
        specs.append(
            ToolSpec(
                name=tool["name"],
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            )
        )
    return specs


def clean_history(history: list[dict] | None) -> list[ChatTurn]:
    """Last N text turns, roles normalised, starting with a user turn."""
    turns: list[ChatTurn] = []
    for item in history or []:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        turns.append(ChatTurn(role, content[:MAX_TURN_CHARS]))
    turns = turns[-MAX_HISTORY_TURNS:]
    while turns and turns[0].role != "user":
        turns.pop(0)
    return turns


def collect_sources(records: list[ToolCallRecord]) -> list[dict[str, Any]]:
    """What the answer was based on: one entry per read-tool call, with each
    metric's value + ``as_of`` so the app can show the data and its age."""
    sources = []
    for rec in records:
        out = rec.output or {}
        if not isinstance(out, dict) or out.get("status") == "awaiting_confirmation":
            continue
        entry: dict[str, Any] = {"tool": rec.name}
        metrics = out.get("metrics")
        if isinstance(metrics, list):
            entry["items"] = [
                {
                    "label": m.get("label"),
                    "value": m.get("value"),
                    "unit": m.get("unit"),
                    "as_of": m.get("as_of"),
                }
                for m in metrics
                if isinstance(m, dict)
            ]
            stamps = [i["as_of"] for i in entry["items"] if i.get("as_of")]
            entry["as_of"] = max(stamps) if stamps else None
        else:
            entry["as_of"] = out.get("last_update_timestamp") or out.get("as_of")
        if out.get("error"):
            entry["error"] = str(out["error"])
        sources.append(entry)
    return sources


class LLMOrchestrator:
    """Answers through an LLM provider; falls back to ``fallback`` on failure."""

    def __init__(self, provider: LLMProvider, fallback: Orchestrator) -> None:
        self.provider = provider
        self.fallback = fallback

    def respond(
        self,
        message: str,
        *,
        run_tool: RunTool,
        context: str | None = None,
        history: list[dict] | None = None,
        zone: dict | None = None,
        language: str | None = None,
        now: datetime.datetime | None = None,
    ) -> AssistantResponse:
        pending: list[dict[str, Any]] = []

        def execute(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
            tool = registry.get(name)
            if tool is None:
                return {"error": f"Unknown tool '{name}'."}
            if tool.mutating:
                if pending:
                    return {"error": "Only one action can be proposed at a time."}
                params = dict(tool_input)
                if zone and "zone_id" in (tool.params or {}):
                    params.setdefault("zone_id", zone.get("id"))
                pending.append({"tool": name, "params": params})
                return {
                    "status": "awaiting_confirmation",
                    "detail": "Not executed. The farmer must confirm it in the app.",
                    "proposed": params,
                }
            return run_tool(name, tool_input)

        try:
            result = self.provider.complete(
                system=SYSTEM_PROMPT,
                context=build_context(
                    now=now or datetime.datetime.now(datetime.timezone.utc),
                    language=language,
                    zone=zone,
                    page=context,
                ),
                history=clean_history(history),
                message=message,
                tools=tool_specs(),
                execute=execute,
            )
        except ProviderError as exc:
            logger.warning("assistant LLM failed (%s): %s", exc.kind, exc.message)
            fb = self.fallback.respond(message, run_tool=run_tool, context=context)
            return LLMAssistantResponse(
                intent=fb.intent,
                tool=fb.tool,
                data=fb.data,
                reply_key=fb.reply_key,
                reply=fb.reply,
                fallback_reason=exc.kind,
            )

        first_read = next(
            (
                r
                for r in result.tool_calls
                if (t := registry.get(r.name)) is not None and not t.mutating
            ),
            None,
        )
        return LLMAssistantResponse(
            intent="llm",
            reply=result.text,
            tool=first_read.name if first_read else None,
            data=first_read.output if first_read else None,
            sources=collect_sources(result.tool_calls),
            pending_action=pending[0] if pending else None,
            model=result.model or None,
        )
