"""Open-Meteo fallback addresses (#550), network-free.

From the production server, DNS sometimes gives a node that stops completing
TLS handshakes while another node still answers. After the normal call (and
its one retry) fails on the network, the client tries each OTHER address of the
host, then the configured ``OPENMETEO_FALLBACK_ADDRESSES``.
"""

from __future__ import annotations

import urllib.error

import pytest

import apps.sensors.forecast_provider as provider

URL = "https://api.open-meteo.com/v1/forecast?latitude=1&longitude=2"


def _dns_fails(monkeypatch):
    calls = []

    def _boom(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError(TimeoutError("_ssl.c:989: handshake timed out"))

    monkeypatch.setattr(provider.urllib.request, "urlopen", _boom)
    return calls


def test_alternates_skip_the_first_dns_answer_then_add_configured(monkeypatch):
    monkeypatch.setattr(
        provider, "_resolved_addresses", lambda host, port: ["10.0.0.1", "10.0.0.2"]
    )
    monkeypatch.setenv(provider._FALLBACK_ADDRESSES_ENV, " 10.0.0.9, 10.0.0.2 ,")

    assert provider._alternate_addresses("api.open-meteo.com", 443) == [
        "10.0.0.2",
        "10.0.0.9",
    ]


def test_a_failing_node_falls_back_to_another_address(monkeypatch):
    calls = _dns_fails(monkeypatch)
    monkeypatch.setattr(
        provider, "_resolved_addresses", lambda host, port: ["10.0.0.1", "10.0.0.2"]
    )
    tried = []

    def _at(url, user_agent, address):
        tried.append(address)
        return {"ok": address}

    monkeypatch.setattr(provider, "_fetch_json_at", _at)

    assert provider._fetch_json(URL, "test") == {"ok": "10.0.0.2"}
    assert len(calls) == provider._FETCH_ATTEMPTS
    assert tried == ["10.0.0.2"]


def test_each_fallback_is_tried_until_one_answers(monkeypatch):
    _dns_fails(monkeypatch)
    monkeypatch.setenv(provider._FALLBACK_ADDRESSES_ENV, "10.0.0.8,10.0.0.9")
    tried = []

    def _at(url, user_agent, address):
        tried.append(address)
        if address == "10.0.0.8":
            raise TimeoutError("still down")
        return {"ok": address}

    monkeypatch.setattr(provider, "_fetch_json_at", _at)

    assert provider._fetch_json(URL, "test") == {"ok": "10.0.0.9"}
    assert tried == ["10.0.0.8", "10.0.0.9"]


def test_the_last_error_is_raised_when_every_address_fails(monkeypatch):
    _dns_fails(monkeypatch)
    monkeypatch.setenv(provider._FALLBACK_ADDRESSES_ENV, "10.0.0.8")

    def _at(url, user_agent, address):
        raise TimeoutError("fallback down too")

    monkeypatch.setattr(provider, "_fetch_json_at", _at)

    with pytest.raises(TimeoutError, match="fallback down too"):
        provider._fetch_json(URL, "test")


def test_without_alternates_the_original_error_is_raised(monkeypatch):
    _dns_fails(monkeypatch)

    with pytest.raises(urllib.error.URLError, match="handshake timed out"):
        provider._fetch_json(URL, "test")


def test_an_http_error_is_not_retried_elsewhere(monkeypatch):
    def _http_error(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(provider.urllib.request, "urlopen", _http_error)
    monkeypatch.setenv(provider._FALLBACK_ADDRESSES_ENV, "10.0.0.8")
    monkeypatch.setattr(
        provider,
        "_fetch_json_at",
        lambda *a: pytest.fail("an HTTP answer must not trigger the fallback"),
    )

    with pytest.raises(urllib.error.HTTPError):
        provider._fetch_json(URL, "test")


def test_the_address_connection_keeps_the_host_name_for_tls():
    conn = provider._AddressHTTPSConnection(
        "api.open-meteo.com", "10.0.0.2", port=443, timeout=1
    )
    assert conn.host == "api.open-meteo.com"
    assert conn._address == "10.0.0.2"
