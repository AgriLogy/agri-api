"""Tests for the stdlib Twilio SMS/WhatsApp sender.

Patches ``urllib.request.urlopen`` and asserts on the request the helper
builds rather than hitting Twilio.
"""

from __future__ import annotations

import os
import urllib.parse
from contextlib import contextmanager
from unittest import mock

from agriapi import twilio_messaging

ENV = {
    "TWILIO_ACCOUNT_SID": "ACxxxxxxxx",
    "TWILIO_AUTH_TOKEN": "tok_secret",
    "TWILIO_SMS_FROM": "+14155551234",
    "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
}


@contextmanager
def _patched(env=ENV, status=201):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def _fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = dict(
            urllib.parse.parse_qsl(req.data.decode("utf-8"))
        )
        return _Resp()

    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch("urllib.request.urlopen", side_effect=_fake):
            yield captured


def test_send_sms_builds_twilio_request():
    with _patched() as cap:
        ok = twilio_messaging.send_sms("+212600000000", "hello")
    assert ok is True
    assert cap["url"] == (
        "https://api.twilio.com/2010-04-01/Accounts/ACxxxxxxxx/Messages.json"
    )
    assert cap["auth"].startswith("Basic ")
    assert cap["body"] == {
        "From": "+14155551234",
        "To": "+212600000000",
        "Body": "hello",
    }


def test_send_whatsapp_prefixes_recipient_and_sender():
    with _patched() as cap:
        ok = twilio_messaging.send_whatsapp("212600000000", "hi")
    assert ok is True
    assert cap["body"]["From"] == "whatsapp:+14155238886"
    assert cap["body"]["To"] == "whatsapp:+212600000000"


def test_send_sms_skips_when_unconfigured():
    env = {k: "" for k in ENV}
    with _patched(env=env) as cap:
        ok = twilio_messaging.send_sms("+212600000000", "hello")
    assert ok is False
    assert cap == {}  # urlopen never called


def test_to_e164_strips_non_digits():
    assert twilio_messaging._to_e164("+212 600-00 00 00") == "+212600000000"
    assert twilio_messaging._to_e164("whatsapp:+212600000000") == "+212600000000"
    assert twilio_messaging._to_e164("") == ""
