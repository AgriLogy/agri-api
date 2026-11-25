"""fastapp /admin/assistant — the assistant's LLM provider settings (#461).

Admin tier only. An admin chooses the provider (Claude, OpenAI/GPT, Groq,
Mistral, Gemini, OpenRouter, DeepSeek, Ollama or any OpenAI-compatible server),
model, base URL, effort, and API key from the back-office instead of env files.
Each provider keeps its own key, so switching back and forth needs no re-paste.
Keys are write-only: stored encrypted, responses only ever show whether one is
set plus its last 4 characters.

* ``GET  /admin/assistant/config``  — current settings, per-provider key status
                                     and the provider catalog
* ``PUT  /admin/assistant/config``  — save (``api_key`` is the chosen provider's:
                                     omitted = keep it, ``""`` = remove it)
* ``POST /admin/assistant/test``    — one small round-trip to the provider with
                                     the saved settings, or with unsaved ones
                                     from the form, to check key/model/URL
* ``POST /admin/assistant/models``  — the model ids the provider offers this key
"""

from __future__ import annotations

import time
from typing import Any

import httpx
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


class AssistantModelsIn(BaseModel):
    """Which provider to ask. Omitted key/URL use the stored / default ones."""

    provider: str
    base_url: str | None = None
    api_key: str | None = None


ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
MODELS_TIMEOUT_S = 10.0


def _require_admin(user: AuthedUser) -> None:
    if level_rank(user.access_level) < level_rank(LEVEL_ADMIN):
        raise HTTPException(status_code=403, detail="Admin access required")


def _public(
    resolved: llm_config.ResolvedConfig, stored: dict[str, Any] | None
) -> dict[str, Any]:
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
        "keys": llm_config.public_keys(stored),
        "providers": PROVIDER_CATALOG,
        "effort_levels": list(EFFORT_LEVELS),
    }


def _bad(detail: str):
    return DjangoStyleJSONResponse({"detail": detail}, status_code=400)


@router.get("/admin/assistant/config", summary="Admin: assistant LLM settings")
def get_config(user: AuthedUser = Depends(get_current_user)):
    _require_admin(user)
    with session_scope() as session:
        return _public(llm_config.resolve(session), llm_config.read_stored(session))


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
        will_have_key = bool(api_key) or (
            api_key is None and payload.provider in llm_config.stored_keys(stored)
        )
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
        return _public(llm_config.resolve(session), llm_config.read_stored(session))


def _stored_key(stored: dict[str, Any], provider: str) -> str:
    return llm_config.key_for(stored, provider) or ""


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
        # The key saved for this provider (each provider has its own).
        api_key = _stored_key(stored, provider)
        if not api_key and provider == saved.provider and saved.provider_config:
            api_key = saved.provider_config.api_key  # env-var fallback
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


@router.post("/admin/assistant/models", summary="Admin: list the provider's models")
def list_models(
    payload: AssistantModelsIn, user: AuthedUser = Depends(get_current_user)
):
    """Ask the provider which models this key can use, so the back-office does
    not depend on a hard-coded list that goes stale."""
    _require_admin(user)
    meta = PROVIDER_CATALOG.get(payload.provider)
    if meta is None:
        return _bad(f"provider must be one of {sorted(PROVIDER_CATALOG)}.")
    api_key = payload.api_key
    if api_key is None:
        with session_scope() as session:
            api_key = _stored_key(
                llm_config.read_stored(session) or {}, payload.provider
            )
    api_key = api_key.strip()
    if meta["openai_compatible"]:
        base_url = (payload.base_url or meta["default_base_url"]).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            return _bad("base_url must start with http:// or https://.")
        url = f"{base_url}/models"
        headers = {"User-Agent": "agri-api-assistant/2.0"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        params: dict[str, Any] = {}
    else:
        url = ANTHROPIC_MODELS_URL
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        params = {"limit": 1000}
        if payload.base_url:
            url = payload.base_url.strip().rstrip("/") + "/v1/models"
    if meta["requires_api_key"] and not api_key:
        return {"ok": False, "error": "No API key saved or given for this provider."}
    try:
        with _models_client() as client:
            res = client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Could not reach the provider: {exc}"}
    if res.status_code in (401, 403):
        return {"ok": False, "error": "The API key was rejected."}
    if res.status_code >= 400:
        return {"ok": False, "error": f"The provider answered HTTP {res.status_code}."}
    try:
        data = res.json().get("data") or []
    except (ValueError, AttributeError):
        return {"ok": False, "error": "The provider did not answer with a model list."}
    models = sorted({str(m["id"]) for m in data if isinstance(m, dict) and m.get("id")})
    return {"ok": True, "models": models}


def _models_client() -> httpx.Client:
    """Separate for tests (they swap in an ``httpx.MockTransport``)."""
    return httpx.Client(timeout=MODELS_TIMEOUT_S)
