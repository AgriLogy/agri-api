"""Voice input: Whisper on Groq behind /assistant/transcribe (#461).

The Groq call goes through an httpx MockTransport; the settings and the Groq
key live in the real ``assistant_llm`` system setting (Postgres).
"""

from __future__ import annotations

import httpx
import pytest
from django.conf import settings as dj_settings
from django.db import connection
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.assistant import config as llm_config
from fastapp.assistant import voice
from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="reads the assistant settings row (needs Postgres)",
)
pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

CONFIG = "/admin/assistant/config"
VOICE_ADMIN = "/admin/assistant/voice"
GROQ_KEY = "gsk_test_0000000000000000000000000000000000000000000000abcd"
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 2048  # EBML magic + padding: a fake clip


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)
    monkeypatch.setattr(get_settings(), "ai_api_key", "")
    yield
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM analytics_systemsetting WHERE key = %s",
            [llm_config.SETTING_KEY],
        )


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(django_user_model, set_access_level):
    user = django_user_model.objects.create_user(
        username="voice-admin", email="voice-admin@example.com", password="x-3921"
    )
    set_access_level(user, "admin")
    return user


@pytest.fixture
def farmer(django_user_model):
    return django_user_model.objects.create_user(
        username="voice-farmer", email="voice-farmer@example.com", password="x-3921"
    )


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


@pytest.fixture
def groq(monkeypatch):
    """Groq transcription endpoint stub; records each request."""
    seen: list[httpx.Request] = []
    reply = {
        "status": 200,
        "json": {
            "text": " شحال خاصني نسقي الزيتون؟ ",
            "language": "arabic",
            "duration": 3.2,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(reply["status"], json=reply["json"])

    monkeypatch.setattr(
        voice, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return {"seen": seen, "reply": reply}


def _setup(fast, admin, *, enabled=True, language="auto", model="whisper-large-v3"):
    # The Groq key is saved on the LLM page (provider Groq), chat disabled.
    r = fast.put(
        CONFIG,
        json={"enabled": False, "provider": "groq", "api_key": GROQ_KEY},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text
    r = fast.put(
        VOICE_ADMIN,
        json={"enabled": enabled, "model": model, "language": language},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _upload(fast, user, *, data=None, content_type="audio/webm", language=None):
    form = {"language": language} if language else {}
    return fast.post(
        "/assistant/transcribe",
        files={
            "file": ("question.webm", data if data is not None else WEBM, content_type)
        },
        data=form,
        headers=_auth(user),
    )


def test_transcribes_with_the_saved_groq_key(fast, admin, farmer, groq):
    pub = _setup(fast, admin)
    assert pub["active"] is True and pub["has_groq_key"] is True
    assert "key" not in pub and GROQ_KEY not in str(pub)

    r = _upload(fast, farmer)
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "شحال خاصني نسقي الزيتون؟"
    assert r.json()["model"] == "whisper-large-v3"

    (req,) = groq["seen"]
    assert str(req.url) == voice.GROQ_TRANSCRIPTIONS_URL
    assert req.headers["authorization"] == f"Bearer {GROQ_KEY}"
    body = req.content
    assert b'name="model"' in body and b"whisper-large-v3" in body
    assert b'name="prompt"' in body and "الزيتون".encode() in body
    assert b'name="language"' not in body  # auto + no chat language: Whisper detects
    assert b'filename="question.webm"' in body


def test_language_follows_the_admin_setting_then_the_chat(fast, admin, farmer, groq):
    _setup(fast, admin, language="auto")
    _upload(fast, farmer, language="ary")  # Darija chat → Arabic script
    assert b'name="language"\r\n\r\nar\r\n' in groq["seen"][-1].content
    _setup(fast, admin, language="fr")
    _upload(fast, farmer, language="ary")  # the admin's choice wins
    assert b'name="language"\r\n\r\nfr\r\n' in groq["seen"][-1].content


def test_not_configured_answers_503_and_status_says_inactive(fast, farmer, groq):
    assert fast.get("/assistant/voice", headers=_auth(farmer)).json()["active"] is False
    r = _upload(fast, farmer)
    assert r.status_code == 503 and r.json()["error_kind"] == "not_configured"
    assert groq["seen"] == []


def test_disabled_voice_is_not_used(fast, admin, farmer, groq):
    _setup(fast, admin, enabled=False)
    assert fast.get("/assistant/voice", headers=_auth(farmer)).json()["active"] is False
    assert _upload(fast, farmer).status_code == 503


def test_rejects_bad_uploads_before_calling_groq(fast, admin, farmer, groq):
    _setup(fast, admin)
    assert _upload(fast, farmer, data=b"").status_code == 400
    assert _upload(fast, farmer, content_type="image/png").status_code == 415
    big = b"\x00" * (voice.MAX_AUDIO_BYTES + 1)
    assert _upload(fast, farmer, data=big).status_code == 413
    assert groq["seen"] == []


def test_provider_errors_are_reported(fast, admin, farmer, groq):
    _setup(fast, admin)
    groq["reply"].update(status=401, json={"error": {"message": "bad key"}})
    r = _upload(fast, farmer)
    assert r.status_code == 502 and r.json()["error_kind"] == "auth"
    groq["reply"].update(status=429, json={})
    assert _upload(fast, farmer).status_code == 429
    groq["reply"].update(status=200, json={"text": "x", "duration": 600})
    assert _upload(fast, farmer).json()["error_kind"] == "too_long"


def test_voice_admin_validation_and_access(fast, admin, farmer):
    h = _auth(admin)
    # No Groq key saved yet: can't enable.
    r = fast.put(VOICE_ADMIN, json={"enabled": True}, headers=h)
    assert r.status_code == 400 and "Groq" in r.json()["detail"]
    assert fast.put(VOICE_ADMIN, json={"model": "nope"}, headers=h).status_code == 400
    assert fast.put(VOICE_ADMIN, json={"language": "xx"}, headers=h).status_code == 400
    assert (
        fast.put(
            VOICE_ADMIN, json={"enabled": False}, headers=_auth(farmer)
        ).status_code
        == 403
    )
    # The voice block shows on the admin config and survives an LLM save.
    _setup(fast, admin, model="whisper-large-v3-turbo")
    fast.put(CONFIG, json={"enabled": False, "provider": "anthropic"}, headers=h)
    cfg = fast.get(CONFIG, headers=h).json()
    assert cfg["voice"]["enabled"] is True
    assert cfg["voice"]["model"] == "whisper-large-v3-turbo"


def test_transcribe_needs_a_login(fast):
    r = fast.post(
        "/assistant/transcribe", files={"file": ("q.webm", WEBM, "audio/webm")}
    )
    assert r.status_code in (401, 403)
