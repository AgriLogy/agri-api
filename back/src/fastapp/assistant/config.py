"""Where the assistant's LLM settings come from (#461).

An admin sets provider / model / base URL / API key / effort in the
back-office (``/admin/assistant/config``). They are stored in
``analytics_systemsetting`` under one reserved key, :data:`SETTING_KEY`, with
the API key **encrypted** (Fernet, key derived from ``SECRET_KEY``) and never
returned by any endpoint: only its last 4 characters are kept for display.

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


def resolve(session) -> ResolvedConfig:
    settings = get_settings()
    stored = read_stored(session)
    if stored is not None:
        provider = stored.get("provider") or ""
        meta = PROVIDER_CATALOG.get(provider, {})
        api_key = ""
        unreadable = False
        if stored.get("api_key_enc"):
            api_key = decrypt_secret(stored["api_key_enc"]) or ""
            unreadable = not api_key
            if unreadable:
                log.error(
                    "assistant LLM API key cannot be decrypted (SECRET_KEY changed?)"
                )
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
            has_api_key=bool(stored.get("api_key_enc")),
            api_key_hint=stored.get("api_key_last4") or "",
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
    """Persist the admin's settings. ``api_key``: None keeps the stored key,
    "" removes it, any other string replaces it (encrypted)."""
    stored = read_stored(session) or {}
    new = {**stored, **values}
    if api_key is not None:
        if api_key:
            new["api_key_enc"] = encrypt_secret(api_key)
            new["api_key_last4"] = api_key[-4:]
        else:
            new.pop("api_key_enc", None)
            new.pop("api_key_last4", None)
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
