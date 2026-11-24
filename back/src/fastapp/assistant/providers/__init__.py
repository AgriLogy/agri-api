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
        "openai_compatible": False,
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "",
        "suggested_models": [],
        "requires_api_key": True,
        "supports_effort": False,
        "openai_compatible": True,
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
        "openai_compatible": True,
    },
    # The presets below all speak the OpenAI chat-completions API. Model lists
    # change often: the back-office loads them live (POST /admin/assistant/models)
    # instead of trusting a hard-coded list; only long-lived aliases are suggested.
    "mistral": {
        "label": "Mistral AI",
        "default_base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-large-latest",
        "suggested_models": ["mistral-large-latest", "mistral-small-latest"],
        "requires_api_key": True,
        "supports_effort": False,
        "openai_compatible": True,
    },
    "gemini": {
        "label": "Google Gemini",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "",
        "suggested_models": [],
        "requires_api_key": True,
        "supports_effort": False,
        "openai_compatible": True,
    },
    "openrouter": {
        "label": "OpenRouter",
        "default_base_url": "https://openrouter.ai/api/v1",
        "default_model": "",
        "suggested_models": [],
        "requires_api_key": True,
        "supports_effort": False,
        "openai_compatible": True,
    },
    "deepseek": {
        "label": "DeepSeek",
        "default_base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "suggested_models": ["deepseek-chat"],
        "requires_api_key": True,
        "supports_effort": False,
        "openai_compatible": True,
    },
    "ollama": {
        "label": "Ollama (self-hosted)",
        "default_base_url": "http://host.docker.internal:11434/v1",
        "default_model": "",
        "suggested_models": [],
        "requires_api_key": False,
        "supports_effort": False,
        "openai_compatible": True,
    },
    # Any other server speaking the OpenAI API (Together, vLLM, LM Studio…).
    "custom": {
        "label": "Other (OpenAI-compatible)",
        "default_base_url": "",
        "default_model": "",
        "suggested_models": [],
        "requires_api_key": False,
        "supports_effort": False,
        "openai_compatible": True,
    },
}

EFFORT_LEVELS = ("low", "medium", "high")


def build_provider(config: ProviderConfig) -> LLMProvider:
    if config.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(config)
    if PROVIDER_CATALOG.get(config.provider, {}).get("openai_compatible"):
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
