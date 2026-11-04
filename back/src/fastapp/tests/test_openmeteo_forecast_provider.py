"""Unit tests for the Open-Meteo daily-weather forecast provider (network-free).

``urlopen`` is monkeypatched, so aggregation, unit handling, the honest
fallback-to-mock (and its ``provider`` label) and the cache are covered offline.
"""

from __future__ import annotations

import datetime
import json

import pytest

import apps.sensors.forecast_provider as provider
from agri.core.et_forecast import et0_forecast

START = datetime.date(2026, 7, 6)


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _hourly_payload(days: int, *, rs_peak: float = 900.0) -> dict:
    """A clear summer day in Morocco, repeated: 24 hourly samples per day."""
    import math

    time, temp, rh, wind, rs, pres = [], [], [], [], [], []
    for d in range(days):
        day = START + datetime.timedelta(days=d)
        for h in range(24):
            time.append(f"{day.isoformat()}T{h:02d}:00")
            temp.append(25.0 + 7.0 * math.sin(2 * math.pi * (h - 9) / 24))
            rh.append(45.0)
            wind.append(4.0)  # m/s at 10 m
            rs.append(
                max(0.0, rs_peak * math.sin(math.pi * (h - 6) / 12))
                if 6 < h < 18
                else 0.0
            )
            pres.append(1000.0)
    return {
        "hourly": {
            "time": time,
            "temperature_2m": temp,
            "relative_humidity_2m": rh,
            "wind_speed_10m": wind,
            "shortwave_radiation": rs,
            "surface_pressure": pres,
        }
    }


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    provider._FORECAST_CACHE.clear()
    monkeypatch.delenv("ET0_OPENMETEO", raising=False)
    monkeypatch.delenv("ET_FORECAST_PROVIDER", raising=False)
    yield
    provider._FORECAST_CACHE.clear()


def _serve(monkeypatch, payload: dict) -> list:
    calls: list = []

    def _urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResp(json.dumps(payload).encode())

    monkeypatch.setattr(provider.urllib.request, "urlopen", _urlopen)
    return calls


def test_openmeteo_is_default_and_aggregates_hourly(monkeypatch):
    calls = _serve(monkeypatch, _hourly_payload(2))
    daily, source = provider.get_daily_forecast_with_source(
        start=START, days=2, latitude=32.9, longitude=-6.9
    )
    assert source == "open-meteo"
    assert [d.day for d in daily] == [START, START + datetime.timedelta(days=1)]
    d0 = daily[0]
    assert d0.temp_c == pytest.approx(25.0, abs=0.1)
    assert d0.rh_pct == 45.0
    assert d0.pressure_hpa == 1000.0
    # 10 m → 2 m (FAO-56 eq. 47 factor ≈ 0.748).
    assert d0.wind_ms == pytest.approx(4.0 * 0.748, abs=0.01)
    # Peak is chosen so the half-sine day carries the forecast energy, which
    # here is itself a 900 W/m² half-sine → peak ≈ 900.
    assert d0.rs_wm2 == pytest.approx(900.0, rel=0.05)
    assert "wind_speed_unit=ms" in calls[0]
    assert "start_date=2026-07-06" in calls[0] and "end_date=2026-07-07" in calls[0]


def test_real_forecast_gives_plausible_summer_et0(monkeypatch):
    _serve(monkeypatch, _hourly_payload(1))
    daily, _ = provider.get_daily_forecast_with_source(
        start=START, days=1, latitude=32.9, longitude=-6.9
    )
    (day,) = et0_forecast(daily, latitude=32.9, longitude=-6.9, elevation_m=300)
    # Hot, dry, sunny Moroccan July day: FAO-56 ET₀ ≈ 5–9 mm/day.
    assert 4.0 <= day["et0_mm"] <= 10.0


def test_cache_avoids_second_request(monkeypatch):
    calls = _serve(monkeypatch, _hourly_payload(3))
    kw = dict(start=START, days=3, latitude=32.9, longitude=-6.9)
    first, _ = provider.get_daily_forecast_with_source(**kw)
    second, _ = provider.get_daily_forecast_with_source(**kw)
    assert first == second
    assert len(calls) == 1


@pytest.mark.parametrize(
    "setup",
    ["network_error", "no_coords", "disabled", "short_payload", "bad_payload"],
)
def test_fallback_is_mock_and_labelled_mock(monkeypatch, setup):
    lat, lon = 32.9, -6.9
    if setup == "network_error":

        def _boom(req, timeout=None):
            raise provider.urllib.error.URLError("down")

        monkeypatch.setattr(provider.urllib.request, "urlopen", _boom)
    elif setup == "no_coords":
        lat = lon = None
    elif setup == "disabled":
        monkeypatch.setenv("ET0_OPENMETEO", "off")
    elif setup == "short_payload":
        _serve(monkeypatch, _hourly_payload(1))  # asked for 3 days
    elif setup == "bad_payload":
        _serve(monkeypatch, {"error": True, "reason": "nope"})

    daily, source = provider.get_daily_forecast_with_source(
        start=START, days=3, latitude=lat, longitude=lon
    )
    assert source == "mock"
    assert daily == provider._mock_forecast(start=START, days=3, latitude=lat)


def test_explicit_mock_never_calls_network(monkeypatch):
    monkeypatch.setenv("ET_FORECAST_PROVIDER", "mock")

    def _fail(req, timeout=None):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(provider.urllib.request, "urlopen", _fail)
    _, source = provider.get_daily_forecast_with_source(
        start=START, days=2, latitude=32.9, longitude=-6.9
    )
    assert source == "mock"


def test_unknown_provider_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("ET_FORECAST_PROVIDER", "openweather")
    _, source = provider.get_daily_forecast_with_source(
        start=START, days=1, latitude=32.9, longitude=-6.9
    )
    assert source == "mock"
