"""Where the assistant's LLM settings come from (#461).

An admin sets provider / model / base URL / API key / effort in the
back-office (``/admin/assistant/config``). They are stored in
``analytics_systemsetting`` under one reserved key, :data:`SETTING_KEY`. Each
provider keeps its own API key (``keys: {provider: {enc, last4}}``), so an admin
can switch between Claude, GPT, Groq… without pasting keys again. Keys are
**encrypted** (Fernet, key derived from ``SECRET_KEY``) and never returned by
any endpoint: only their last 4 characters are kept for display. A legacy
single ``api_key_enc`` (before per-provider keys) counts as the key of the
stored provider and is moved under ``keys`` on the next save.

Resolution order:

1. the back-office setting, when present (``enabled: false`` turns the LLM off
   and the assistant falls back to the rule-based commands);
2. legacy ``AI_API_KEY`` / ``AI_API_BASE_URL`` / ``AI_MODEL`` env vars
   (OpenAI-compatible), so existing deployments keep working;
3. nothing → rule-based assistant.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from agri.db.audit import AnalyticsSystemsetting
from fastapp.settings import get_settings

from .providers import EFFORT_LEVELS, PROVIDER_CATALOG, ProviderConfig

log = logging.getLogger("fastapp.assistant.config")

SETTING_KEY = "assistant_llm"
SETTING_CATEGORY = "assistant"
# Keys the generic /admin/settings endpoints must neither show nor write.
RESERVED_SETTING_KEYS = frozenset({SETTING_KEY})


def _fernet() -> Fernet:
    digest = hashlib.sha256(
        b"agri-api/assistant-llm/v1:" + get_settings().secret_key.encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str | None:
    """None when the token can't be read (e.g. SECRET_KEY was rotated)."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


@dataclass(frozen=True)
class ResolvedConfig:
    provider_config: ProviderConfig | None  # None = LLM off
    source: str  # "settings" | "env" | "none"
    enabled: bool
    provider: str = ""
    model: str = ""
    base_url: str = ""
    effort: str = "low"
    has_api_key: bool = False
    api_key_hint: str = ""
    key_unreadable: bool = False
    updated_at: str | None = None
    updated_by: str | None = None


def read_stored(session) -> dict[str, Any] | None:
    row = session.scalars(
        select(AnalyticsSystemsetting).where(AnalyticsSystemsetting.key == SETTING_KEY)
    ).first()
    return dict(row.value) if row is not None and isinstance(row.value, dict) else None


def stored_keys(stored: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """``{provider: {"enc": ..., "last4": ...}}``, legacy single key included."""
    stored = stored or {}
    keys = {
        p: dict(v)
        for p, v in (stored.get("keys") or {}).items()
        if isinstance(v, dict) and v.get("enc")
    }
    legacy_provider = stored.get("provider") or ""
    if stored.get("api_key_enc") and legacy_provider and legacy_provider not in keys:
        keys[legacy_provider] = {
            "enc": stored["api_key_enc"],
            "last4": stored.get("api_key_last4") or "",
        }
    return keys


def key_for(stored: dict[str, Any] | None, provider: str) -> str | None:
    """The decrypted key saved for ``provider``: "" when none is saved, None
    when one is saved but can't be decrypted (SECRET_KEY changed)."""
    entry = stored_keys(stored).get(provider)
    if not entry:
        return ""
    return decrypt_secret(entry["enc"])


def public_keys(stored: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Per-provider key status for the back-office: never the key itself."""
    return {
        p: {"has_key": True, "hint": v.get("last4") or ""}
        for p, v in stored_keys(stored).items()
    }


def resolve(session) -> ResolvedConfig:
    settings = get_settings()
    stored = read_stored(session)
    if stored is not None:
        provider = stored.get("provider") or ""
        meta = PROVIDER_CATALOG.get(provider, {})
        decrypted = key_for(stored, provider)
        api_key = decrypted or ""
        unreadable = decrypted is None
        if unreadable:
            log.error("assistant LLM API key cannot be decrypted (SECRET_KEY changed?)")
        enabled = bool(stored.get("enabled"))
        base_url = stored.get("base_url") or meta.get("default_base_url", "")
        model = stored.get("model") or meta.get("default_model", "")
        effort = (
            stored.get("effort") if stored.get("effort") in EFFORT_LEVELS else "low"
        )
        usable = (
            enabled
            and provider in PROVIDER_CATALOG
            and bool(model)
            and (bool(api_key) or not meta.get("requires_api_key"))
        )
        return ResolvedConfig(
            provider_config=ProviderConfig(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                timeout=float(settings.ai_timeout or 30),
                effort=effort,
            )
            if usable
            else None,
            source="settings",
            enabled=enabled,
            provider=provider,
            model=model,
            base_url=base_url,
            effort=effort,
            has_api_key=provider in stored_keys(stored),
            api_key_hint=(stored_keys(stored).get(provider) or {}).get("last4", ""),
            key_unreadable=unreadable,
            updated_at=stored.get("updated_at"),
            updated_by=stored.get("updated_by"),
        )
    if settings.ai_api_key:
        return ResolvedConfig(
            provider_config=ProviderConfig(
                provider="openai",
                model=settings.ai_model,
                api_key=settings.ai_api_key,
                base_url=settings.ai_api_base_url,
                timeout=float(settings.ai_timeout or 30),
            ),
            source="env",
            enabled=True,
            provider="openai",
            model=settings.ai_model,
            base_url=settings.ai_api_base_url,
            has_api_key=True,
            api_key_hint=settings.ai_api_key[-4:],
        )
    return ResolvedConfig(provider_config=None, source="none", enabled=False)


def save(
    session, *, values: dict[str, Any], api_key: str | None, username: str
) -> None:
    """Persist the admin's settings. ``api_key`` is for ``values["provider"]``:
    None keeps its stored key, "" removes it, any other string replaces it
    (encrypted). The other providers' keys are kept as they are."""
    stored = read_stored(session) or {}
    keys = stored_keys(stored)  # migrates a legacy single key under ``keys``
    new = {**stored, **values}
    new.pop("api_key_enc", None)
    new.pop("api_key_last4", None)
    provider = new.get("provider") or ""
    if api_key is not None and provider:
        if api_key:
            keys[provider] = {"enc": encrypt_secret(api_key), "last4": api_key[-4:]}
        else:
            keys.pop(provider, None)
    new["keys"] = keys
    now = datetime.datetime.now(datetime.timezone.utc)
    new["updated_at"] = now.isoformat()
    new["updated_by"] = username
    row = session.scalars(
        select(AnalyticsSystemsetting).where(AnalyticsSystemsetting.key == SETTING_KEY)
    ).first()
    if row is None:
        session.add(
            AnalyticsSystemsetting(
                key=SETTING_KEY, value=new, category=SETTING_CATEGORY, updated_at=now
            )
        )
    else:
        row.value = new
        row.updated_at = now
    session.flush()
