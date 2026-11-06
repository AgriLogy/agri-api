"""LLM backends for the assistant. ``build_provider(config)`` picks one.

``PROVIDER_CATALOG`` is what the back-office shows: which providers exist,
their default base URL / model, and whether an API key is required.
"""

from __future__ import annotations

from .base import (
    ChatTurn,
    LLMProvider,
    LLMResult,
    ProviderConfig,
    ProviderError,
    ToolCallRecord,
    ToolSpec,
)

PROVIDER_CATALOG: dict[str, dict] = {
    "anthropic": {
        "label": "Claude (Anthropic)",
        "default_base_url": "",
        "default_model": "claude-opus-5-5",
        "suggested_models": [
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
        ],
        "requires_api_key": True,
        "supports_effort": True,
    },
    "openai": {
        "label": "OpenAI-compatible (OpenAI, OpenRouter, Together…)",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "",
        "suggested_models": [],
        "requires_api_key": True,
        "supports_effort": False,
    },
    # Groq speaks the OpenAI API; a preset so the admin only pastes a key.
    # Production models with tool use (console.groq.com/docs/models, 2026-11).
    "groq": {
        "label": "Groq",
        "default_base_url": "https://api.groq.com/openai/v1",
        "default_model": "openai/gpt-oss-120b",
        "suggested_models": [
            "openai/gpt-oss-120b",
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-20b",
            "llama-3.1-8b-instant",
        ],
        "requires_api_key": True,
        "supports_effort": False,
    },
    "ollama": {
        "label": "Ollama (self-hosted)",
        "default_base_url": "http://host.docker.internal:11434/v1",
        "default_model": "",
        "suggested_models": [],
        "requires_api_key": False,
        "supports_effort": False,
    },
}

EFFORT_LEVELS = ("low", "medium", "high")


def build_provider(config: ProviderConfig) -> LLMProvider:
    if config.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(config)
    if config.provider in ("openai", "groq", "ollama"):
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(config)
    raise ProviderError("config", f"Unknown provider '{config.provider}'.")


__all__ = [
    "EFFORT_LEVELS",
    "PROVIDER_CATALOG",
    "ChatTurn",
    "LLMProvider",
    "LLMResult",
    "ProviderConfig",
    "ProviderError",
    "ToolCallRecord",
    "ToolSpec",
    "build_provider",
]
