"""Unit tests for the Open-Meteo reference-ET₀ fetch (network-free).

The live call is best-effort and keyless; here we monkeypatch ``urlopen`` so the
parsing, the disable switch, the missing-coordinate guard, and the
failure-is-empty contract are all covered without touching the network.
"""

from __future__ import annotations

import datetime
import json

import apps.sensors.forecast_provider as provider


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_parses_daily_et0_and_rounds(monkeypatch):
    monkeypatch.delenv("ET0_OPENMETEO", raising=False)
    payload = {
        "daily": {
            "time": ["2026-07-06", "2026-07-07"],
            "et0_fao_evapotranspiration": [5.12, 6.004],
        }
    }
    monkeypatch.setattr(
        provider.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp(json.dumps(payload).encode()),
    )
    out = provider.fetch_openmeteo_et0(
        start=datetime.date(2026, 7, 6), days=2, latitude=32.9, longitude=-6.9
    )
    assert out == {"2026-07-06": 5.12, "2026-07-07": 6.0}


def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("ET0_OPENMETEO", "off")
    assert (
        provider.fetch_openmeteo_et0(
            start=datetime.date(2026, 7, 6), days=2, latitude=1.0, longitude=2.0
        )
        == {}
    )


def test_missing_coordinates_returns_empty(monkeypatch):
    monkeypatch.delenv("ET0_OPENMETEO", raising=False)
    assert (
        provider.fetch_openmeteo_et0(
            start=datetime.date(2026, 7, 6), days=2, latitude=None, longitude=None
        )
        == {}
    )


def test_network_error_returns_empty(monkeypatch):
    monkeypatch.delenv("ET0_OPENMETEO", raising=False)

    def _boom(req, timeout=None):
        raise provider.urllib.error.URLError("open-meteo down")

    monkeypatch.setattr(provider.urllib.request, "urlopen", _boom)
    assert (
        provider.fetch_openmeteo_et0(
            start=datetime.date(2026, 7, 6), days=1, latitude=1.0, longitude=2.0
        )
        == {}
    )


def test_null_values_are_skipped(monkeypatch):
    monkeypatch.delenv("ET0_OPENMETEO", raising=False)
    payload = {
        "daily": {
            "time": ["2026-07-06", "2026-07-07"],
            "et0_fao_evapotranspiration": [None, 4.5],
        }
    }
    monkeypatch.setattr(
        provider.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp(json.dumps(payload).encode()),
    )
    out = provider.fetch_openmeteo_et0(
        start=datetime.date(2026, 7, 6), days=2, latitude=32.9, longitude=-6.9
    )
    assert out == {"2026-07-07": 4.5}
