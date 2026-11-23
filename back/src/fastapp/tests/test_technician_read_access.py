"""Technician read access to sectors, the farm-map sector reads and water
sources.

A technician with an active ``TechnicianGrant`` reads the owner's data through
the shared scope (``routers/selfreads._resolve_read_scope``), narrowed to the
granted zones:

* sectors that contain at least one granted zone (list, overview, insights,
  forecast, shape history, NDVI reads);
* water sources whose ``zone_ids`` intersect the granted zones.

Every write stays refused, and a sector / source outside the grant, of another
farmer, or behind an inactive grant answers like it does not exist.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings
from django.db import connection
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp import sector_insights as insights_mod
from fastapp.main import app
from fastapp.routers import ndvi as ndvi_router
from fastapp.schema_compat import reset_sector_geometry_cache, reset_table_cache
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM: fastapp reads the test DB Django writes (needs Postgres)",
)
pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [
            [-6.915, 32.889],
            [-6.913, 32.889],
            [-6.913, 32.891],
            [-6.915, 32.891],
            [-6.915, 32.889],
        ]
    ],
}

BASIN = {
    "source_type": "BASIN",
    "geometry": "RECTANGULAR",
    "max_height_m": 4.8,
    "length_m": 8,
    "width_m": 3,
    "minimum_reserve_m3": 10,
}


class _FakeCDSE:
    def acquisitions(self, geometry, start, end, max_cloud):
        return [{"date": "2026-10-06", "cloud_cover": 3.0}]

    def ndvi_stats(self, geometry, start, end, kind="ndvi"):
        return [{"date": end.isoformat(), "mean": 0.6, "min": 0.2, "max": 0.8}]

    def ndvi_png(self, geometry, date, index):
        return b"\x89PNG fake"


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)
    monkeypatch.setattr(ndvi_router, "get_client", lambda: _FakeCDSE())
    monkeypatch.setattr(insights_mod, "fetch_openmeteo_daily_water", lambda **kw: {})
    monkeypatch.setenv("ET_FORECAST_PROVIDER", "mock")
    reset_sector_geometry_cache()
    reset_table_cache()
    yield TestClient(app)
    with connection.cursor() as cursor:  # no Django model → never flushed
        cursor.execute(
            "TRUNCATE water_source, water_level_device, water_level_reading, "
            "water_level_alert"
        )


def _user(django_user_model, set_access_level, name, **extra):
    u = django_user_model.objects.create_user(
        username=name, email=f"{name}@example.com", password="x-29481", **extra
    )
    set_access_level(u, "editor")
    return u


def _auth(u):
    return {"Authorization": f"Bearer {AccessToken.for_user(u)}"}


def _sector(api, user, name) -> int:
    res = api.post(
        "/sectors", json={"name": name, "geometry": SQUARE}, headers=_auth(user)
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _zone(owner, name, sector_id):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=owner,
        name=name,
        space=2000.0,
        critical_moisture_threshold=25.0,
        pomp_flow_rate=5.0,
        elevation_m=100.0,
        irrigation_water_quantity=1000.0,
        sector_id=sector_id,
    )


def _source(api, user, name, zone_ids) -> int:
    res = api.post(
        "/water-sources",
        json={**BASIN, "name": name, "zone_ids": zone_ids},
        headers=_auth(user),
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


@pytest.fixture
def farm(api, django_user_model, set_access_level):
    """Owner with sectors A (granted + ungranted zone), B (ungranted zone),
    C (no zone); a technician granted A's first zone; another farmer."""
    from apps.irrigation.models import TechnicianGrant, TechnicianZoneGrant

    owner = _user(django_user_model, set_access_level, "ta-owner")
    other = _user(django_user_model, set_access_level, "ta-other")
    tech = _user(django_user_model, set_access_level, "ta-tech", is_technician=True)
    a = _sector(api, owner, "A")
    b = _sector(api, owner, "B")
    c = _sector(api, owner, "C")
    za = _zone(owner, "ZA", a)
    za2 = _zone(owner, "ZA2", a)
    zb = _zone(owner, "ZB", b)
    foreign = _sector(api, other, "Foreign")
    zf = _zone(other, "ZF", foreign)
    grant = TechnicianGrant.objects.create(owner=owner, technician=tech)
    TechnicianZoneGrant.objects.create(grant=grant, zone=za, allowed_graphs=[])
    return {
        "owner": owner,
        "other": other,
        "tech": tech,
        "grant": grant,
        "a": a,
        "b": b,
        "c": c,
        "foreign": foreign,
        "za": za.id,
        "za2": za2.id,
        "zb": zb.id,
        "zf": zf.id,
    }


def _sector_reads(sid: int) -> list[str]:
    return [
        f"/farm-map/sectors/{sid}/insights",
        f"/sectors/{sid}/geometry-history",
        f"/farm-map/ndvi/sectors/{sid}/dates",
        f"/farm-map/ndvi/sectors/{sid}/stats",
        f"/farm-map/ndvi/sectors/{sid}/history",
        f"/farm-map/ndvi/sectors/{sid}/image?date=2026-10-06",
    ]


# --------------------------------------------------------------------- sectors
def test_technician_lists_only_sectors_with_a_granted_zone(api, farm):
    tech = _auth(farm["tech"])
    body = api.get("/sectors", headers=tech).json()
    assert [s["id"] for s in body] == [farm["a"]]
    # Same shape as the owner's row; zone_count counts the granted zones.
    owner_row = next(
        s
        for s in api.get("/sectors", headers=_auth(farm["owner"])).json()
        if s["id"] == farm["a"]
    )
    assert body[0].keys() == owner_row.keys()
    assert body[0]["geometry"] == SQUARE and body[0]["area_ha"] > 0
    assert owner_row["zone_count"] == 2 and body[0]["zone_count"] == 1


def test_technician_overview_holds_granted_zones_only(api, farm):
    body = api.get("/sectors/overview", headers=_auth(farm["tech"])).json()
    assert [(s["sector_id"], [z["zone_id"] for z in s["zones"]]) for s in body] == [
        (farm["a"], [farm["za"]])
    ]


def test_technician_reads_granted_sector(api, farm):
    tech = _auth(farm["tech"])
    for path in _sector_reads(farm["a"]):
        res = api.get(path, headers=tech)
        assert res.status_code == 200, (path, res.text)
    insights = api.get(f"/farm-map/sectors/{farm['a']}/insights", headers=tech).json()
    assert insights["zone_ids"] == [farm["za"]]
    history = api.get(f"/sectors/{farm['a']}/geometry-history", headers=tech).json()
    assert [r["source"] for r in history["revisions"]] == ["draw"]
    forecast = api.get("/farm-map/forecast", headers=tech).json()
    assert [s["sector_id"] for s in forecast["sectors"]] == [farm["a"]]
    # The owner still sees all three drawn sectors.
    owner_forecast = api.get("/farm-map/forecast", headers=_auth(farm["owner"])).json()
    assert sorted(s["sector_id"] for s in owner_forecast["sectors"]) == sorted(
        [farm["a"], farm["b"], farm["c"]]
    )


@pytest.mark.parametrize("which", ["b", "c", "foreign"])
def test_technician_cannot_read_ungranted_or_foreign_sector(api, farm, which):
    tech = _auth(farm["tech"])
    for path in _sector_reads(farm[which]):
        res = api.get(path, headers=tech)
        assert res.status_code == 404, (path, res.text)


def test_inactive_grant_sees_nothing(api, farm):
    farm["grant"].is_active = False
    farm["grant"].save()
    tech = _auth(farm["tech"])
    assert api.get("/sectors", headers=tech).json() == []
    assert api.get("/sectors/overview", headers=tech).json() == []
    assert api.get("/farm-map/forecast", headers=tech).json()["sectors"] == []
    for path in _sector_reads(farm["a"]):
        assert api.get(path, headers=tech).status_code == 404, path


def test_another_farmer_cannot_read_owner_sectors(api, farm):
    other = _auth(farm["other"])
    assert [s["id"] for s in api.get("/sectors", headers=other).json()] == [
        farm["foreign"]
    ]
    for path in _sector_reads(farm["a"]):
        assert api.get(path, headers=other).status_code == 404, path


def test_technician_sector_writes_stay_refused(api, farm):
    tech = _auth(farm["tech"])
    a = farm["a"]
    rev = api.get(f"/sectors/{a}/geometry-history", headers=tech).json()["revisions"][
        0
    ]["id"]
    for method, path, kw in (
        ("post", "/sectors", {"json": {"name": "X"}}),
        ("patch", f"/sectors/{a}", {"json": {"name": "X"}}),
        ("delete", f"/sectors/{a}", {}),
        ("put", f"/sectors/{a}/zones", {"json": {"zone_ids": [farm["za"]]}}),
        ("post", f"/sectors/{a}/geometry-history/{rev}/restore", {}),
        (
            "post",
            "/farm-map/sectors/import-kml",
            {"files": {"file": ("f.kml", b"<kml/>")}, "data": {"dry_run": "false"}},
        ),
    ):
        res = api.request(method, path, headers=tech, **kw)
        assert res.status_code in (403, 400), (method, path, res.text)
        if path != "/farm-map/sectors/import-kml":
            assert res.status_code == 403, (method, path, res.text)
    owner_rows = api.get("/sectors", headers=_auth(farm["owner"])).json()
    assert sorted(s["name"] for s in owner_rows) == ["A", "B", "C"]


# ---------------------------------------------------------------- water sources
@pytest.fixture
def sources(api, farm):
    owner = farm["owner"]
    return {
        "granted": _source(api, owner, "Bassin principal", [farm["za"], farm["zb"]]),
        "ungranted": _source(api, owner, "Puits B", [farm["zb"]]),
        "no_zone": _source(api, owner, "Citerne", []),
        "foreign": _source(api, farm["other"], "Bassin voisin", [farm["zf"]]),
    }


def _source_reads(sid: int) -> list[str]:
    return [
        f"/water-sources/{sid}",
        f"/water-sources/{sid}/status",
        f"/water-sources/{sid}/history?range=7d",
        f"/water-sources/{sid}/alerts",
    ]


def test_technician_lists_sources_on_granted_zones(api, farm, sources):
    tech = _auth(farm["tech"])
    body = api.get("/water-sources", headers=tech).json()
    assert [s["id"] for s in body] == [sources["granted"]]
    assert body[0]["name"] == "Bassin principal" and "status" in body[0]
    for path in _source_reads(sources["granted"]):
        res = api.get(path, headers=tech)
        assert res.status_code == 200, (path, res.text)
    # The owner keeps seeing all of theirs.
    owner = api.get("/water-sources", headers=_auth(farm["owner"])).json()
    assert len(owner) == 3


@pytest.mark.parametrize("which", ["ungranted", "no_zone", "foreign"])
def test_technician_cannot_read_other_sources(api, farm, sources, which):
    tech = _auth(farm["tech"])
    for path in _source_reads(sources[which]):
        assert api.get(path, headers=tech).status_code == 404, path


def test_inactive_grant_sees_no_source(api, farm, sources):
    farm["grant"].is_active = False
    farm["grant"].save()
    tech = _auth(farm["tech"])
    assert api.get("/water-sources", headers=tech).json() == []
    for path in _source_reads(sources["granted"]):
        assert api.get(path, headers=tech).status_code == 404, path


def test_another_farmer_cannot_read_owner_sources(api, farm, sources):
    other = _auth(farm["other"])
    assert [s["id"] for s in api.get("/water-sources", headers=other).json()] == [
        sources["foreign"]
    ]
    for path in _source_reads(sources["granted"]):
        assert api.get(path, headers=other).status_code == 404, path


def test_technician_source_writes_stay_refused(api, farm, sources):
    tech = _auth(farm["tech"])
    sid = sources["granted"]
    for method, path, body in (
        ("post", "/water-sources", {**BASIN, "name": "X"}),
        ("patch", f"/water-sources/{sid}", {"name": "X"}),
        ("delete", f"/water-sources/{sid}", None),
        ("post", f"/water-sources/{sid}/simulate", {"level_m": 1}),
        ("post", "/water-level/readings", {"water_source_id": sid, "level_m": 1}),
    ):
        res = api.request(method, path, json=body, headers=tech)
        assert res.status_code == 403, (method, path, res.text)
    src = api.get(f"/water-sources/{sid}", headers=_auth(farm["owner"])).json()
    assert src["name"] == "Bassin principal"
    status = api.get(f"/water-sources/{sid}/status", headers=_auth(farm["owner"]))
    assert status.json().get("last_reading") is None
