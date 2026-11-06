"""OpenAI-compatible backend for the assistant (#461).

One client for every server that speaks the OpenAI chat-completions API:
OpenAI itself, Groq (``groq`` preset), OpenRouter, Together, and Ollama (which serves the same
API under ``http://<host>:11434/v1``). Plain HTTP via ``httpx`` so no
vendor SDK is pulled in for what is a generic protocol.

Tool loop: the model returns ``tool_calls``; each result goes back as a
``role: "tool"`` message keyed by ``tool_call_id``; repeat until the model
answers in text (capped at ``MAX_TOOL_ROUNDS``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

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

log = logging.getLogger("fastapp.assistant.openai_compat")


class OpenAICompatProvider:
    def __init__(self, config: ProviderConfig, http: httpx.Client | None = None):
        if not config.base_url:
            raise ProviderError("config", "A base URL is required for this provider.")
        self.config = config
        headers = {
            "Content-Type": "application/json",
            # Some edges (e.g. Groq's WAF) reject the default python agent.
            "User-Agent": "agri-api-assistant/2.0",
        }
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self.http = http or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=config.timeout,
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
            {"role": "system", "content": system},
            {"role": "system", "content": context},
        ]
        messages += [{"role": t.role, "content": t.content} for t in history]
        messages.append({"role": "user", "content": message})
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        records: list[ToolCallRecord] = []
        usage: dict[str, int] = {}
        reply: dict[str, Any] = {}
        model = self.config.model

        for _round in range(MAX_TOOL_ROUNDS + 1):
            payload: dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "max_tokens": self.config.max_output_tokens,
                "temperature": 0.2,
            }
            # On the last allowed round, stop offering tools so the model must
            # answer with what it already has.
            if tool_defs and _round < MAX_TOOL_ROUNDS:
                payload["tools"] = tool_defs
                payload["tool_choice"] = "auto"
            body = self._post(payload)
            model = body.get("model") or model
            for k in ("prompt_tokens", "completion_tokens"):
                v = (body.get("usage") or {}).get(k)
                if isinstance(v, int):
                    usage[k] = usage.get(k, 0) + v
            try:
                reply = body["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderError(
                    "unavailable", "Malformed response from the model server."
                ) from exc

            tool_calls = reply.get("tool_calls") or []
            if not tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                try:
                    tool_input = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    tool_input = {}
                if not isinstance(tool_input, dict):
                    tool_input = {}
                try:
                    output = execute(name, tool_input)
                except Exception:
                    log.exception("assistant tool %s failed", name)
                    output = {"error": f"The {name} lookup failed."}
                records.append(ToolCallRecord(name, tool_input, output))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        # No "name": Groq rejects messages[].name with a 400,
                        # and tool_call_id is all OpenAI needs.
                        "content": json.dumps(output, default=str, ensure_ascii=False),
                    }
                )

        return LLMResult(
            text=(reply.get("content") or "").strip(),
            tool_calls=records,
            model=model,
            usage=usage,
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self.http.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError("unavailable", "The model server timed out.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "unavailable", "Could not reach the model server."
            ) from exc
        if resp.status_code in (401, 403):
            raise ProviderError("auth", "The API key was rejected.")
        if resp.status_code == 404:
            raise ProviderError(
                "bad_request", f"Unknown model '{self.config.model}' or wrong base URL."
            )
        if resp.status_code == 429:
            raise ProviderError(
                "rate_limit", "The model server rate limit was reached."
            )
        if resp.status_code >= 500:
            raise ProviderError(
                "unavailable", f"Model server error {resp.status_code}."
            )
        if resp.status_code >= 400:
            raise ProviderError("bad_request", _error_text(resp))
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(
                "unavailable", "Non-JSON response from the model server."
            ) from exc


def _error_text(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if err:
            return str(err)[:300]
    except ValueError:
        pass
    return f"Model server rejected the request ({resp.status_code})."
