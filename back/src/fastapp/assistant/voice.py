"""Voice input for the assistant: speech → text with Whisper on Groq (#461).

The farmer records a question in the chat; the web app uploads the audio to
``POST /assistant/transcribe``, which sends it to Groq's OpenAI-compatible
``/audio/transcriptions`` endpoint and returns the text. The text then goes
through the normal chat, like a typed message.

Settings live in the assistant's ``assistant_llm`` system setting under
``voice`` (``{"enabled", "model", "language"}``), edited from the back-office
(``PUT /admin/assistant/voice``). The Groq API key is the one saved for the
``groq`` provider on the same page (one key per provider), so voice works even
when the chat itself runs on another provider.

Language: Whisper has no Darija code. ``ar`` makes it write Arabic script,
which is what Darija speakers get from it; ``auto`` lets it detect (better
when farmers mix Darija and French). A short prompt of farm words biases the
spelling of the terms farmers use (crop names, vanne, goutte-à-goutte…).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import httpx

from . import config as llm_config

GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
VOICE_PROVIDER = "groq"
VOICE_MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
DEFAULT_MODEL = "whisper-large-v3"
VOICE_LANGUAGES = ("auto", "ar", "fr", "en")
DEFAULT_LANGUAGE = "auto"

#: Upload limits: a spoken question is seconds long; Groq's free tier takes
#: files up to 25 MB, a minute of Opus/WebM is well under 1 MB.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_SECONDS = 120
TIMEOUT_S = 30.0

ALLOWED_TYPES = frozenset(
    {
        "audio/webm",
        "audio/ogg",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/flac",
        "video/webm",  # some browsers label a voice-only MediaRecorder blob so
    }
)

#: Vocabulary hint (Whisper ``prompt``): words farmers say, in the scripts the
#: transcript should use. Kept short: Whisper only reads the last ~224 tokens.
VOCABULARY_PROMPT = (
    "سقي، الما، الضيعة، الزيتون، البطاطا، القمح، الليمون، الفلاحة، الرطوبة، "
    "الحرارة، الشتا، الصمام، الپومپ، goutte-à-goutte, vanne, pompe, secteur, "
    "irrigation, ET0, m³, mm."
)

# Chat language (app setting) → Whisper language when the admin chose "auto".
_CHAT_TO_WHISPER = {"ary": "ar", "ar": "ar", "fr": "fr", "en": "en"}


@dataclass(frozen=True)
class VoiceSettings:
    enabled: bool
    model: str
    language: str
    has_key: bool
    key: str  # decrypted Groq key; "" when none

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.key)

    def public(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": VOICE_PROVIDER,
            "model": self.model,
            "language": self.language,
            "has_groq_key": self.has_key,
            "active": self.usable,
            "models": list(VOICE_MODELS),
            "languages": list(VOICE_LANGUAGES),
            "max_seconds": MAX_SECONDS,
        }


class VoiceError(Exception):
    def __init__(self, kind: str, message: str, status: int = 502):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


def read_settings(session) -> VoiceSettings:
    stored = llm_config.read_stored(session) or {}
    voice = stored.get("voice") if isinstance(stored.get("voice"), dict) else {}
    model = voice.get("model") if voice.get("model") in VOICE_MODELS else DEFAULT_MODEL
    language = (
        voice.get("language")
        if voice.get("language") in VOICE_LANGUAGES
        else DEFAULT_LANGUAGE
    )
    key = llm_config.key_for(stored, VOICE_PROVIDER) or ""
    return VoiceSettings(
        enabled=bool(voice.get("enabled")),
        model=model,
        language=language,
        has_key=VOICE_PROVIDER in llm_config.stored_keys(stored),
        key=key,
    )


def save_settings(
    session, *, enabled: bool, model: str, language: str, username: str
) -> None:
    """Store the voice block, keeping every other assistant setting."""
    from sqlalchemy import select

    from agri.db.audit import AnalyticsSystemsetting

    now = datetime.datetime.now(datetime.timezone.utc)
    row = session.scalars(
        select(AnalyticsSystemsetting).where(
            AnalyticsSystemsetting.key == llm_config.SETTING_KEY
        )
    ).first()
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    value["voice"] = {
        "enabled": enabled,
        "model": model,
        "language": language,
        "updated_at": now.isoformat(),
        "updated_by": username,
    }
    if row is None:
        session.add(
            AnalyticsSystemsetting(
                key=llm_config.SETTING_KEY,
                value=value,
                category=llm_config.SETTING_CATEGORY,
                updated_at=now,
            )
        )
    else:
        row.value = value
        row.updated_at = now
    session.flush()


def whisper_language(setting: str, chat_language: str | None) -> str | None:
    """The ``language`` sent to Whisper: the admin's choice, else the chat's
    language when the farmer set one, else None (Whisper detects)."""
    if setting != "auto":
        return setting
    return _CHAT_TO_WHISPER.get(chat_language or "")


def _http() -> httpx.Client:
    """Separate for tests (they swap in an ``httpx.MockTransport``)."""
    return httpx.Client(timeout=TIMEOUT_S)


def transcribe(
    settings: VoiceSettings,
    audio: bytes,
    *,
    filename: str,
    content_type: str,
    chat_language: str | None = None,
) -> dict[str, Any]:
    """Send ``audio`` to Whisper on Groq. Returns ``{text, language, model}``."""
    if not settings.usable:
        raise VoiceError(
            "not_configured",
            "Voice input is not set up (back-office: Assistant IA → Voix, and a "
            "Groq API key).",
            503,
        )
    if not audio:
        raise VoiceError("empty", "The recording is empty.", 400)
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError("too_large", "The recording is too long.", 413)
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind not in ALLOWED_TYPES:
        raise VoiceError("bad_type", f"Unsupported audio type '{kind}'.", 415)

    data = {
        "model": settings.model,
        "response_format": "verbose_json",
        "temperature": "0",
        "prompt": VOCABULARY_PROMPT,
    }
    language = whisper_language(settings.language, chat_language)
    if language:
        data["language"] = language
    try:
        with _http() as client:
            resp = client.post(
                GROQ_TRANSCRIPTIONS_URL,
                headers={"Authorization": f"Bearer {settings.key}"},
                data=data,
                files={"file": (filename or "voice.webm", audio, kind)},
            )
    except httpx.TimeoutException as exc:
        raise VoiceError(
            "timeout", "The voice service did not answer in time."
        ) from exc
    except httpx.HTTPError as exc:
        raise VoiceError("network", "The voice service could not be reached.") from exc
    if resp.status_code == 401:
        raise VoiceError("auth", "The Groq API key was rejected.")
    if resp.status_code == 429:
        raise VoiceError(
            "rate_limit", "Too many voice requests; try again shortly.", 429
        )
    if resp.status_code >= 400:
        raise VoiceError("provider", f"The voice service failed ({resp.status_code}).")
    body = resp.json()
    duration = body.get("duration")
    if isinstance(duration, (int, float)) and duration > MAX_SECONDS:
        raise VoiceError("too_long", "The recording is too long.", 413)
    return {
        "text": (body.get("text") or "").strip(),
        "language": body.get("language") or language,
        "duration_s": duration,
        "model": settings.model,
    }
