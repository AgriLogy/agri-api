"""Claude backend for the assistant, via the official Anthropic SDK (#461).

Manual tool loop (not the SDK tool runner) because tool execution must go
through our callback: it enforces per-user scoping and the confirm-before-write
gate. Request shape notes for current Claude models:

* no ``temperature``: sampling params are rejected on current models;
* thinking is left at the model default (adaptive) and depth is set with
  ``output_config.effort`` (chat answers do well at ``low``);
* forced ``tool_choice`` is rejected on current models, so ``auto`` + prompt;
* the stable system block carries ``cache_control`` so the long instruction
  prompt + tool list are cached; per-request context goes after it;
* on models that support it, server-side refusal fallback is enabled
  (``fallbacks="default"``) so a policy decline is retried on a fallback model
  inside the same call instead of failing the farmer's question.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from .base import (
    MAX_TOOL_ROUNDS,
    ChatTurn,
    ExecuteTool,
    LLMResult,
    ProviderConfig,
    ProviderError,
    ToolCallRecord,
    ToolSpec,
)

log = logging.getLogger("fastapp.assistant.anthropic")

# Models that accept the scalar server-side fallback form.
_FALLBACK_MODELS = {
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    def __init__(
        self, config: ProviderConfig, client: anthropic.Anthropic | None = None
    ):
        if not config.api_key:
            raise ProviderError("config", "Anthropic API key is not set.")
        self.config = config
        self.client = client or anthropic.Anthropic(
            api_key=config.api_key,
            base_url=config.base_url or None,
            timeout=config.timeout,
            max_retries=2,  # SDK retries 408/409/429/5xx with backoff
        )

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
        messages: list[dict[str, Any]] = [
            {"role": t.role, "content": t.content} for t in history
        ]
        messages.append({"role": "user", "content": message})
        system_blocks = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": context},
        ]
        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        records: list[ToolCallRecord] = []
        usage: dict[str, int] = {}
        response = None

        for _round in range(MAX_TOOL_ROUNDS + 1):
            response = self._create(system_blocks, messages, tool_defs)
            _add_usage(usage, response.usage)

            if response.stop_reason == "refusal":
                raise ProviderError("refusal", "The model declined to answer.")
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break
            if _round == MAX_TOOL_ROUNDS:
                log.warning("assistant tool-round cap reached (%s)", MAX_TOOL_ROUNDS)
                break

            # Append the full assistant content (tool_use + any thinking blocks)
            # unchanged, then ALL tool results in ONE user message.
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in tool_uses:
                tool_input = block.input if isinstance(block.input, dict) else {}
                output, is_error = _run(execute, block.name, tool_input)
                records.append(ToolCallRecord(block.name, tool_input, output))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(output, default=str, ensure_ascii=False),
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})

        text = "".join(
            b.text for b in (response.content if response else []) if b.type == "text"
        ).strip()
        if response is not None and response.stop_reason == "max_tokens" and not text:
            raise ProviderError("bad_request", "The answer hit the output-token limit.")
        return LLMResult(
            text=text,
            tool_calls=records,
            model=getattr(response, "model", self.config.model),
            usage=usage,
        )

    def _create(self, system_blocks, messages, tool_defs):
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_output_tokens,
            "system": system_blocks,
            "messages": messages,
            "output_config": {"effort": self.config.effort},
        }
        if tool_defs:
            kwargs["tools"] = tool_defs
        try:
            if self.config.model in _FALLBACK_MODELS:
                return self.client.beta.messages.create(
                    betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
                )
            return self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise ProviderError("auth", "The Anthropic API key was rejected.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError(
                "auth", "The API key lacks permission for this model."
            ) from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(
                "bad_request", f"Unknown model '{self.config.model}'."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("rate_limit", "Anthropic rate limit reached.") from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError("bad_request", exc.message) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                "unavailable", f"Anthropic error {exc.status_code}."
            ) from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise ProviderError(
                "unavailable", "Could not reach the Anthropic API."
            ) from exc


def _run(execute: ExecuteTool, name: str, tool_input: dict) -> tuple[dict, bool]:
    try:
        output = execute(name, tool_input)
    except Exception:
        log.exception("assistant tool %s failed", name)
        return {"error": f"The {name} lookup failed."}, True
    return output, bool(isinstance(output, dict) and output.get("error"))


def _add_usage(acc: dict[str, int], usage) -> None:
    if usage is None:
        return
    for field_name in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = getattr(usage, field_name, None)
        if isinstance(value, int):
            acc[field_name] = acc.get(field_name, 0) + value
