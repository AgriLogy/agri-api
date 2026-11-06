"""fastapp /admin/assistant — the assistant's LLM provider settings (#461).

Admin tier only. An admin chooses the provider (Claude, OpenAI-compatible,
Ollama), model, base URL, effort, and API key from the back-office instead of
env files. The API key is write-only: it is stored encrypted and responses
only ever show whether one is set plus its last 4 characters.

* ``GET  /admin/assistant/config``  — current settings + provider catalog
* ``PUT  /admin/assistant/config``  — save (``api_key`` omitted = keep it,
                                     ``""`` = remove it)
* ``POST /admin/assistant/test``    — one small round-trip to the provider with
                                     the saved settings, or with unsaved ones
                                     from the form, to check key/model/URL
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agri.core.database import session_scope
from fastapp.adminutil import record_audit
from fastapp.assistant import config as llm_config
from fastapp.assistant.providers import (
    EFFORT_LEVELS,
    PROVIDER_CATALOG,
    ProviderConfig,
    ProviderError,
    build_provider,
)
from fastapp.auth import LEVEL_ADMIN, AuthedUser, get_current_user, level_rank
from fastapp.json import DjangoStyleJSONResponse
from fastapp.settings import get_settings

router = APIRouter(tags=["admin-assistant"])

_TEST_SYSTEM = "You are a connectivity check. Reply with exactly: OK"


class AssistantConfigIn(BaseModel):
    enabled: bool = False
    provider: str
    model: str = ""
    base_url: str = ""
    effort: str = "low"
    # None/omitted keeps the stored key; "" removes it; a string replaces it.
    api_key: str | None = None


class AssistantTestIn(BaseModel):
    """Optional unsaved settings to test. Omitted fields use the saved ones."""

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    effort: str | None = None
    api_key: str | None = None


def _require_admin(user: AuthedUser) -> None:
    if level_rank(user.access_level) < level_rank(LEVEL_ADMIN):
        raise HTTPException(status_code=403, detail="Admin access required")


def _public(resolved: llm_config.ResolvedConfig) -> dict[str, Any]:
    return {
        "source": resolved.source,
        "enabled": resolved.enabled,
        "active": resolved.provider_config is not None,
        "provider": resolved.provider,
        "model": resolved.model,
        "base_url": resolved.base_url,
        "effort": resolved.effort,
        "has_api_key": resolved.has_api_key,
        "api_key_hint": resolved.api_key_hint,
        "api_key_unreadable": resolved.key_unreadable,
        "updated_at": resolved.updated_at,
        "updated_by": resolved.updated_by,
        "providers": PROVIDER_CATALOG,
        "effort_levels": list(EFFORT_LEVELS),
    }


def _bad(detail: str):
    return DjangoStyleJSONResponse({"detail": detail}, status_code=400)


@router.get("/admin/assistant/config", summary="Admin: assistant LLM settings")
def get_config(user: AuthedUser = Depends(get_current_user)):
    _require_admin(user)
    with session_scope() as session:
        return _public(llm_config.resolve(session))


@router.put("/admin/assistant/config", summary="Admin: save assistant LLM settings")
def put_config(
    payload: AssistantConfigIn, user: AuthedUser = Depends(get_current_user)
):
    _require_admin(user)
    meta = PROVIDER_CATALOG.get(payload.provider)
    if meta is None:
        return _bad(f"provider must be one of {sorted(PROVIDER_CATALOG)}.")
    if payload.effort not in EFFORT_LEVELS:
        return _bad(f"effort must be one of {list(EFFORT_LEVELS)}.")
    model = payload.model.strip() or meta["default_model"]
    base_url = payload.base_url.strip() or meta["default_base_url"]
    if payload.enabled and not model:
        return _bad("model is required to enable the assistant.")
    if payload.provider != "anthropic" and payload.enabled and not base_url:
        return _bad("base_url is required for this provider.")
    if base_url and not base_url.startswith(("http://", "https://")):
        return _bad("base_url must start with http:// or https://.")
    api_key = payload.api_key.strip() if payload.api_key is not None else None
    with session_scope(commit=True) as session:
        stored = llm_config.read_stored(session) or {}
        will_have_key = bool(api_key) or (api_key is None and stored.get("api_key_enc"))
        if payload.enabled and meta["requires_api_key"] and not will_have_key:
            return _bad("an API key is required for this provider.")
        llm_config.save(
            session,
            values={
                "enabled": payload.enabled,
                "provider": payload.provider,
                "model": model,
                "base_url": base_url,
                "effort": payload.effort,
            },
            api_key=api_key,
            username=user.username,
        )
        # Never log the key itself: only whether it changed.
        record_audit(
            session,
            user.id,
            "assistant_llm.update",
            "settings",
            llm_config.SETTING_KEY,
            {
                "enabled": payload.enabled,
                "provider": payload.provider,
                "model": model,
                "base_url": base_url,
                "effort": payload.effort,
                "api_key": "replaced"
                if api_key
                else ("removed" if api_key == "" else "kept"),
            },
        )
        return _public(llm_config.resolve(session))


@router.post("/admin/assistant/test", summary="Admin: test the assistant LLM settings")
def test_config(payload: AssistantTestIn, user: AuthedUser = Depends(get_current_user)):
    _require_admin(user)
    with session_scope() as session:
        saved = llm_config.resolve(session)
        stored = llm_config.read_stored(session) or {}
    provider = payload.provider or saved.provider
    meta = PROVIDER_CATALOG.get(provider)
    if meta is None:
        return _bad("Choose a provider first.")
    api_key = payload.api_key
    if api_key is None:
        # Reuse the saved key only for the provider it was saved for.
        api_key = ""
        if provider == stored.get("provider") and stored.get("api_key_enc"):
            api_key = llm_config.decrypt_secret(stored["api_key_enc"]) or ""
        elif provider == saved.provider and saved.provider_config:
            api_key = saved.provider_config.api_key
    cfg = ProviderConfig(
        provider=provider,
        model=(
            payload.model
            or (saved.model if provider == saved.provider else "")
            or meta["default_model"]
        ).strip(),
        api_key=api_key.strip(),
        base_url=(
            payload.base_url
            or (saved.base_url if provider == saved.provider else "")
            or meta["default_base_url"]
        ).strip(),
        timeout=float(get_settings().ai_timeout or 30),
        effort=payload.effort or saved.effort or "low",
        max_output_tokens=64,
    )
    if not cfg.model:
        return _bad("Choose a model first.")
    started = time.monotonic()
    try:
        result = build_provider(cfg).complete(
            system=_TEST_SYSTEM,
            context="Connectivity test from the Agrogo back-office.",
            history=[],
            message="Reply with OK.",
            tools=[],
            execute=lambda name, args: {},
        )
    except ProviderError as exc:
        return {
            "ok": False,
            "error_kind": exc.kind,
            "error": exc.message,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "ok": True,
        "model": result.model or cfg.model,
        "reply": result.text[:200],
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
