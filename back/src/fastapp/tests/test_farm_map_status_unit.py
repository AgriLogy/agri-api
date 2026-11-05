"""DB-free unit tests for the farm-map status rules and request validation.

The status colour is what a farmer reads off the map, so its edges — stale vs
offline, an alert on a dead sensor, a sensor with no stream — are pinned here
on every machine (the HTTP suite needs Postgres).
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from fastapp.routers.farm_map import (
    InstallationIn,
    MapPreferencesIn,
    classify_status,
    worst_status,
)
from fastapp.routers.sectors import SectorIn

NOW = dt.datetime(2026, 11, 4, 12, 0, tzinfo=dt.timezone.utc)


def _ago(**kw) -> dt.datetime:
    return NOW - dt.timedelta(**kw)


@pytest.mark.parametrize(
    ("ts", "triggered", "expected"),
    [
        (_ago(minutes=10), False, "normal"),
        (_ago(hours=6), False, "normal"),  # edge: exactly STALE_AFTER
        (_ago(hours=7), False, "warning"),
        (_ago(hours=24), False, "warning"),  # edge: exactly OFFLINE_AFTER
        (_ago(hours=25), False, "offline"),
        (_ago(minutes=10), True, "critical"),
        (_ago(hours=7), True, "critical"),
        # A triggered alert on a dead sensor is evaluating a stale value.
        (_ago(days=3), True, "offline"),
        (None, False, "no_data"),
        (None, True, "no_data"),
    ],
)
def test_classify_status(ts, triggered, expected):
    assert classify_status(ts, now=NOW, triggered=triggered) == expected


def test_unlinked_sensor_is_grey_not_no_data():
    assert classify_status(None, now=NOW, linked=False) == "unlinked"


def test_naive_timestamps_are_treated_as_utc():
    naive = (NOW - dt.timedelta(minutes=5)).replace(tzinfo=None)
    assert classify_status(naive, now=NOW) == "normal"


def test_worst_status_rolls_up_most_severe():
    assert worst_status(["normal", "warning", "no_data"]) == "warning"
    assert worst_status(["normal", "offline", "critical"]) == "critical"
    assert worst_status(["unlinked", "no_data"]) == "no_data"
    assert worst_status([]) == "no_data"


def test_installation_rejects_unknown_sensor_key():
    with pytest.raises(ValidationError):
        InstallationIn(
            name="SM-01",
            sensor_type="soil_moisture",
            latitude=32.9,
            longitude=-6.9,
            sensor_key="not_a_sensor",
        )


@pytest.mark.parametrize(("lat", "lon"), [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_installation_rejects_out_of_range_coordinates(lat, lon):
    with pytest.raises(ValidationError):
        InstallationIn(
            name="x", sensor_type="soil_moisture", latitude=lat, longitude=lon
        )


def test_installation_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        InstallationIn(
            name="x", sensor_type="soil_moisture", latitude=0, longitude=0, colour="red"
        )


def test_map_preferences_validation():
    ok = MapPreferencesIn(
        basemap="satellite",
        layers={"ndvi": {"visible": False, "opacity": 0.4}},
        camera={"center": [-6.9, 32.9], "zoom": 15},
    )
    assert ok.layers["ndvi"].opacity == 0.4
    with pytest.raises(ValidationError):
        MapPreferencesIn(basemap="google")
    with pytest.raises(ValidationError):
        MapPreferencesIn(layers={"sectors": {"visible": True, "opacity": 1.5}})
    with pytest.raises(ValidationError):
        MapPreferencesIn(camera={"center": [200, 0], "zoom": 10})


def test_sector_metadata_only_sends_present_fields():
    rename_only = SectorIn(name="Block A")
    assert rename_only.metadata_updates() == {}

    full = SectorIn(
        name="Block A",
        crop="  Avocado ",
        irrigation_type="drip",
        root_depth_cm=60,
        is_active=None,
    )
    assert full.metadata_updates() == {
        "crop": "Avocado",
        "irrigation_type": "drip",
        "root_depth_cm": 60,
        "is_active": True,  # NOT NULL column: null means default
    }

    cleared = SectorIn(name="Block A", crop="   ")
    assert cleared.metadata_updates() == {"crop": None}


def test_sector_rejects_unknown_irrigation_type():
    with pytest.raises(ValidationError):
        SectorIn(name="x", irrigation_type="hose")
