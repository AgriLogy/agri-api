"""Unit tests for the sector-geometry write path (no database).

``test_sectors.py`` covers /sectors over HTTP, but that suite needs Postgres
(dual-ORM) and is skipped everywhere else. The rules that actually decide
whether a farmer's polygon survives a rename live in ``_apply_geometry``, and
they are pure enough to test directly — so they are, here, on every machine.

The geodesic maths itself belongs to ``agri.core.geometry`` and is tested there
against a closed-form area; these tests only assert that the router calls it
and stores what it returns.
"""

from __future__ import annotations

from typing import Any

import pytest
from agri.core.geometry import area_hectares, perimeter_m

from fastapp.routers import sectors as mod

_SQUARE: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [
        [
            [-7.95, 32.20],
            [-7.94, 32.20],
            [-7.94, 32.21],
            [-7.95, 32.21],
            [-7.95, 32.20],
        ]
    ],
}


class _Sector:
    """Stand-in for AnalyticsSector: attribute bag, no ORM, no DB."""

    def __init__(self, **kw: Any) -> None:
        self.name = kw.get("name", "S1")
        self.geometry = kw.get("geometry")
        self.area_ha = kw.get("area_ha")
        self.perimeter_m = kw.get("perimeter_m")
        self.color = kw.get("color")
        self.geometry_updated_at = kw.get("geometry_updated_at")


@pytest.fixture
def available(monkeypatch):
    """Schema has the geometry columns."""
    monkeypatch.setattr(mod, "sector_geometry_available", lambda _s: True)


@pytest.fixture
def unavailable(monkeypatch):
    """Schema predates migration b8c2f0d5e713."""
    monkeypatch.setattr(mod, "sector_geometry_available", lambda _s: False)


def _apply(sector: _Sector, **payload: Any):
    return mod._apply_geometry(object(), sector, mod.SectorIn(**payload))


# ---------------------------------------------------------------------------
# Derived values
# ---------------------------------------------------------------------------
def test_geometry_write_derives_area_and_perimeter(available) -> None:
    s = _Sector()
    assert _apply(s, name="S1", geometry=_SQUARE) is None
    assert s.geometry["type"] == "Polygon"
    # Derived from the stored shape by agri-core — asserted against that
    # function rather than a copied constant, because what this test is for is
    # "the router derives them from the geometry", not the geodesy (which is
    # pinned to a closed-form area in agri-core's own suite).
    assert s.area_ha == pytest.approx(area_hectares(_SQUARE))
    assert s.perimeter_m == pytest.approx(perimeter_m(_SQUARE))
    # Independent order-of-magnitude guard so a wrong-function wiring (area
    # given the perimeter, say) cannot pass: 0.01 deg at 32.2 N is a
    # ~0.94 km x ~1.11 km box, so ~105 ha and a ~4.1 km boundary.
    assert 100.0 < s.area_ha < 110.0
    assert 4000.0 < s.perimeter_m < 4200.0
    assert s.geometry_updated_at is not None


def test_client_supplied_area_is_refused_outright() -> None:
    """area_ha is derived; accepting it would let the number drift from the
    shape, which is the bug this whole move exists to prevent."""
    with pytest.raises(Exception):
        mod.SectorIn(name="S1", area_ha=999.0)


def test_invalid_geometry_is_a_400_and_leaves_the_sector_untouched(available) -> None:
    s = _Sector(geometry=_SQUARE, area_ha=116.0)
    resp = _apply(s, name="S1", geometry={"type": "Point", "coordinates": [0, 0]})
    assert resp is not None and resp.status_code == 400
    assert s.geometry == _SQUARE and s.area_ha == 116.0


def test_swapped_lat_lon_is_rejected(available) -> None:
    swapped = {
        "type": "Polygon",
        "coordinates": [[[32.2, 200.0], [32.3, 200.0], [32.3, 201.0], [32.2, 200.0]]],
    }
    resp = _apply(_Sector(), name="S1", geometry=swapped)
    assert resp is not None and resp.status_code == 400


# ---------------------------------------------------------------------------
# The three-state field: omitted / null / geometry
# ---------------------------------------------------------------------------
def test_rename_without_geometry_keeps_the_existing_shape(available) -> None:
    """The regression that matters: renaming must never erase a polygon."""
    s = _Sector(geometry=_SQUARE, area_ha=116.0, perimeter_m=4330.0, color="#abcdef")
    assert _apply(s, name="Renamed") is None
    assert s.geometry == _SQUARE
    assert s.area_ha == 116.0
    assert s.perimeter_m == 4330.0
    assert s.color == "#abcdef"


def test_explicit_null_erases_the_shape_and_its_numbers(available) -> None:
    s = _Sector(geometry=_SQUARE, area_ha=116.0, perimeter_m=4330.0)
    assert _apply(s, name="S1", geometry=None) is None
    assert s.geometry is None
    assert s.area_ha is None
    assert s.perimeter_m is None
    # An erase is still a shape change.
    assert s.geometry_updated_at is not None


def test_color_alone_does_not_touch_the_shape(available) -> None:
    s = _Sector(geometry=_SQUARE, area_ha=116.0)
    assert _apply(s, name="S1", color="#123456") is None
    assert s.color == "#123456"
    assert s.geometry == _SQUARE
    assert s.geometry_updated_at is None


# ---------------------------------------------------------------------------
# Pre-migration deployments
# ---------------------------------------------------------------------------
def test_shape_write_on_unmigrated_schema_is_503(unavailable) -> None:
    resp = _apply(_Sector(), name="S1", geometry=_SQUARE)
    assert resp is not None and resp.status_code == 503


def test_plain_rename_still_works_on_unmigrated_schema(unavailable) -> None:
    assert _apply(_Sector(), name="Renamed") is None


def test_erasing_an_absent_shape_is_a_noop_not_a_503(unavailable) -> None:
    """`geometry: null` asks for a state the un-migrated schema is already in;
    failing that would block the front from saving a plain rename."""
    assert _apply(_Sector(), name="S1", geometry=None) is None
