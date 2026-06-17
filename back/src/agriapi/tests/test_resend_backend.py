"""Tests for the Resend HTTP email backend.

The backend is stdlib-only and talks to https://api.resend.com/emails, so we
patch ``urllib.request.urlopen`` and assert on the request it builds rather
than hitting the network.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest import mock

import pytest
from django.core.mail import EmailMultiAlternatives, send_mail
from django.test import override_settings

from agriapi.email_backends import ResendEmailBackend

RESEND_BACKEND = "agriapi.email_backends.ResendEmailBackend"


@contextmanager
def _patched_urlopen():
    """Patch urlopen to capture the outgoing request and fake a response."""
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id": "fake-id"}'

    def _fake(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    with mock.patch("urllib.request.urlopen", side_effect=_fake):
        yield captured


@override_settings(EMAIL_BACKEND=RESEND_BACKEND, RESEND_API_KEY="re_test_key")
def test_send_mail_posts_to_resend():
    with _patched_urlopen() as cap:
        sent = send_mail(
            subject="Hi",
            message="Body text",
            from_email="Agrilogy <noreply@agrogo-datafarm.com>",
            recipient_list=["z.mks.iii@gmail.com"],
            fail_silently=False,
        )
    assert sent == 1
    req = cap["req"]
    assert req.full_url == "https://api.resend.com/emails"
    assert req.get_header("Authorization") == "Bearer re_test_key"
    # Explicit UA — Resend is behind Cloudflare, which 403s Python-urllib.
    assert req.get_header("User-agent") == "agri-api-resend-backend/1.0"
    body = cap["body"]
    assert body["from"] == "Agrilogy <noreply@agrogo-datafarm.com>"
    assert body["to"] == ["z.mks.iii@gmail.com"]
    assert body["subject"] == "Hi"
    assert body["text"] == "Body text"


@override_settings(EMAIL_BACKEND=RESEND_BACKEND, RESEND_API_KEY="re_test_key")
def test_html_alternative_included():
    msg = EmailMultiAlternatives(
        subject="S", body="plain", from_email="a@b.com", to=["c@d.com"]
    )
    msg.attach_alternative("<b>hi</b>", "text/html")
    with _patched_urlopen() as cap:
        sent = msg.send()
    assert sent == 1
    assert cap["body"]["html"] == "<b>hi</b>"


@override_settings(
    EMAIL_BACKEND=RESEND_BACKEND, RESEND_API_KEY="re_test_key", EMAIL_TIMEOUT=7
)
def test_respects_email_timeout():
    with _patched_urlopen() as cap:
        send_mail("s", "m", "a@b.com", ["c@d.com"])
    assert cap["timeout"] == 7


def test_missing_api_key_raises_when_not_silent():
    backend = ResendEmailBackend(fail_silently=False)
    backend.api_key = ""
    with pytest.raises(RuntimeError):
        backend.send_messages([mock.Mock()])


def test_missing_api_key_silent_returns_zero():
    backend = ResendEmailBackend(fail_silently=True)
    backend.api_key = ""
    assert backend.send_messages([mock.Mock()]) == 0
