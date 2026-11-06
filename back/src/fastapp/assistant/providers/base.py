"""Provider-neutral contract for the assistant's LLM backends (#461).

The orchestrator talks only to :class:`LLMProvider`; each backend (Claude via
the Anthropic SDK, OpenAI-compatible HTTP for OpenAI / Groq / OpenRouter /
Ollama) owns its own wire format and tool-calling loop, because those formats
differ (Anthropic ``tool_use``/``tool_result`` content blocks vs OpenAI
``tool_calls`` + ``role: tool`` messages).

Tool execution stays on our side: the provider calls ``execute(name, input)``
and feeds the returned dict back to the model. Authorization, per-user scoping
and the confirm-before-write gate all live in that callback, never in a
provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# Hard ceiling on model→tool→model rounds per user message. A farm question
# rarely needs more than 2-3 lookups; the cap bounds cost and latency.
MAX_TOOL_ROUNDS = 5


@dataclass(frozen=True)
class ChatTurn:
    """One prior conversation turn, text only (role: "user" | "assistant")."""

    role: str
    content: str


@dataclass(frozen=True)
class ToolSpec:
    """A tool as offered to the model: JSON-schema parameters, provider-neutral."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCallRecord:
    """A tool the model asked for during this turn, and what it got back."""

    name: str
    input: dict[str, Any]
    output: dict[str, Any]


@dataclass
class LLMResult:
    text: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)


# (tool name, tool input) -> JSON-serialisable result fed back to the model.
ExecuteTool = Callable[[str, dict[str, Any]], dict[str, Any]]


class ProviderError(Exception):
    """A backend failure the caller can act on.

    ``kind`` is one of: ``auth`` (bad/missing key), ``rate_limit``,
    ``unavailable`` (network, timeout, 5xx), ``bad_request`` (model name,
    payload), ``refusal`` (the model declined), ``config`` (not configured).
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class ProviderConfig:
    provider: str  # "anthropic" | "openai" | "ollama"
    model: str
    api_key: str = ""
    base_url: str = ""
    timeout: float = 30.0
    # Anthropic only: output_config.effort ("low" | "medium" | "high").
    effort: str = "low"
    max_output_tokens: int = 2048


class LLMProvider(Protocol):
    def complete(
        self,
        *,
        system: str,
        context: str,
        history: list[ChatTurn],
        message: str,
        tools: list[ToolSpec],
        execute: ExecuteTool,
    ) -> LLMResult:
        """Answer ``message`` given prior ``history``.

        ``system`` is the stable instruction block (cacheable); ``context`` is
        the per-request part (date, selected zone, page) and must be sent after
        it so prompt caching keeps working.
        """
        ...
