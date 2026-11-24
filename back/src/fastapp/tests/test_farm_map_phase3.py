"""Farm map phase 3 (agri-api#459): KML import, shape history, stored NDVI,
sector insights (water balance + advice + NDVI × soil) and the forecast layer.

KML parsing is unit-tested; the router tests are dual-ORM (Postgres).
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp import sector_insights as insights_mod
from fastapp.kml import KmlError, parse_kml
from fastapp.main import app
from fastapp.routers import ndvi as ndvi_router
from fastapp.satellite.store import geometry_hash
from fastapp.schema_compat import reset_sector_geometry_cache, reset_table_cache
from fastapp.settings import get_settings

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
SQUARE_2 = {
    "type": "Polygon",
    "coordinates": [
        [
            [-6.915, 32.889],
            [-6.912, 32.889],
            [-6.912, 32.891],
            [-6.915, 32.891],
            [-6.915, 32.889],
        ]
    ],
}

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark><name>Bloc A</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
    -6.915,32.889,0 -6.913,32.889,0 -6.913,32.891,0 -6.915,32.891,0 -6.915,32.889,0
  </coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
  <Placemark><name>Bloc B</name><MultiGeometry>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>
      -6.910,32.889 -6.909,32.889 -6.909,32.890
    </coordinates></LinearRing></outerBoundaryIs></Polygon>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>
      -6.905,32.889 -6.904,32.889 -6.904,32.890 -6.905,32.889
    </coordinates></LinearRing></outerBoundaryIs></Polygon>
  </MultiGeometry></Placemark>
  <Placemark><name>Puits</name><Point><coordinates>-6.91,32.89</coordinates></Point></Placemark>
  <Placemark><name>Cassé</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
    nope
  </coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>""".encode()


# ---------------------------------------------------------------------- KML
def test_parse_kml_polygons_multigeometry_and_skips():
    shapes = parse_kml(KML).shapes
    assert [s.name for s in shapes] == ["Bloc A", "Bloc B", "Puits", "Cassé"]
    a, b, point, broken = shapes
    assert a.geometry["type"] == "Polygon" and a.geometry["coordinates"][0][0] == [
        -6.915,
        32.889,
    ]
    # Open rings are closed; both rings of the MultiGeometry are kept.
    assert b.geometry["type"] == "MultiPolygon" and len(b.geometry["coordinates"]) == 2
    assert b.geometry["coordinates"][0][0][0] == b.geometry["coordinates"][0][0][-1]
    assert point.geometry is None and point.reason == "not_a_polygon"
    assert broken.geometry is None and broken.reason == "invalid_geometry"


def test_parse_kmz():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.kml", KML)
    assert len(parse_kml(buf.getvalue(), "farm.kmz").shapes) == 4


@pytest.mark.parametrize(
    "data",
    [
        b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaa">]><kml>&a;</kml>',
        b"<kml><Placemark>",
        b"<gpx></gpx>",
        b"x" * (2 * 1024 * 1024 + 1),
    ],
    # Explicit ids: pytest would otherwise turn the 2 MB payload into a 2 MB
    # test id, and that single -v log line stalled the GitHub Actions job.
    ids=["dtd", "broken-xml", "not-kml", "over-2mb"],
)
def test_parse_kml_refuses_bad_files(data):
    with pytest.raises(KmlError):
        parse_kml(data)


def test_geometry_hash_ignores_key_order():
    flipped = {"coordinates": SQUARE["coordinates"], "type": "Polygon"}
    assert geometry_hash(flipped) == geometry_hash(SQUARE)
    assert geometry_hash(SQUARE) != geometry_hash(SQUARE_2)


# ------------------------------------------------------------------- router
_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM: fastapp reads the test DB Django writes (needs Postgres)",
)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)
    reset_sector_geometry_cache()
    reset_table_cache()
    return TestClient(app)


@pytest.fixture
def owner(django_user_model, set_access_level):
    u = django_user_model.objects.create_user(
        username="p3-owner", email="p3-owner@example.com", password="x-29481"
    )
    set_access_level(u, "editor")
    return u


@pytest.fixture
def other(django_user_model, set_access_level):
    u = django_user_model.objects.create_user(
        username="p3-other", email="p3-other@example.com", password="x-29481"
    )
    set_access_level(u, "editor")
    return u


def _auth(u):
    return {"Authorization": f"Bearer {AccessToken.for_user(u)}"}


def _sector(api, user, name="S1", geometry=SQUARE) -> int:
    res = api.post(
        "/sectors", json={"name": name, "geometry": geometry}, headers=_auth(user)
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["id"]


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_shape_history_and_restore(api, owner, other):
    sid = _sector(api, owner)
    h = api.get(f"/sectors/{sid}/geometry-history", headers=_auth(owner)).json()
    assert [r["source"] for r in h["revisions"]] == ["draw"]

    api.patch(
        f"/sectors/{sid}",
        json={"name": "S1", "geometry": SQUARE_2},
        headers=_auth(owner),
    )
    # Same shape again and a plain rename add nothing.
    api.patch(
        f"/sectors/{sid}",
        json={"name": "S1", "geometry": SQUARE_2},
        headers=_auth(owner),
    )
    api.patch(f"/sectors/{sid}", json={"name": "S1 bis"}, headers=_auth(owner))
    revs = api.get(f"/sectors/{sid}/geometry-history", headers=_auth(owner)).json()[
        "revisions"
    ]
    assert len(revs) == 2 and revs[0]["geometry"] == SQUARE_2  # newest first
    assert revs[1]["area_ha"] > 0

    res = api.post(
        f"/sectors/{sid}/geometry-history/{revs[1]['id']}/restore", headers=_auth(owner)
    )
    assert res.status_code == 200, res.text
    assert res.json()["geometry"] == SQUARE and res.json()["name"] == "S1 bis"
    revs = api.get(f"/sectors/{sid}/geometry-history", headers=_auth(owner)).json()[
        "revisions"
    ]
    assert [r["source"] for r in revs] == ["restore", "draw", "draw"]

    # Another farmer sees nothing and cannot restore.
    assert (
        api.get(f"/sectors/{sid}/geometry-history", headers=_auth(other)).status_code
        == 404
    )
    res = api.post(
        f"/sectors/{sid}/geometry-history/{revs[1]['id']}/restore", headers=_auth(other)
    )
    assert res.status_code == 404


class _FakeCDSE:
    def acquisitions(self, geometry, start, end, max_cloud):
        return [{"date": "2026-10-06", "cloud_cover": 3.0}]

    def ndvi_stats(self, geometry, start, end, kind="ndvi"):
        today = dt.datetime.now(dt.UTC).date()
        return [
            {
                "date": (today - dt.timedelta(days=d)).isoformat(),
                "mean": m,
                "min": 0.1,
                "max": 0.9,
                "std": 0.1,
                "valid_fraction": 0.95,
                "classes": {"low": 0.1, "medium": 0.5, "high": 0.4},
            }
            for d, m in ((20, 0.70), (15, 0.62), (10, 0.55), (5, 0.48))
        ]


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_ndvi_history_is_stored_per_shape(api, owner, monkeypatch):
    monkeypatch.setattr(ndvi_router, "get_client", lambda: _FakeCDSE())
    sid = _sector(api, owner)
    empty = api.get(
        f"/farm-map/ndvi/sectors/{sid}/history", headers=_auth(owner)
    ).json()
    assert empty["series"] == []

    assert (
        api.get(f"/farm-map/ndvi/sectors/{sid}/stats", headers=_auth(owner)).status_code
        == 200
    )
    # A second fetch upserts, never duplicates.
    api.get(f"/farm-map/ndvi/sectors/{sid}/stats", headers=_auth(owner))
    api.get(f"/farm-map/ndvi/sectors/{sid}/dates", headers=_auth(owner))
    hist = api.get(f"/farm-map/ndvi/sectors/{sid}/history", headers=_auth(owner)).json()
    assert [p["mean"] for p in hist["series"]] == [0.70, 0.62, 0.55, 0.48]
    assert hist["series"][0]["classes"] == {"low": 0.1, "medium": 0.5, "high": 0.4}

    # Redrawn: the old numbers belong to the old shape.
    api.patch(
        f"/sectors/{sid}",
        json={"name": "S1", "geometry": SQUARE_2},
        headers=_auth(owner),
    )
    hist = api.get(f"/farm-map/ndvi/sectors/{sid}/history", headers=_auth(owner)).json()
    assert hist["series"] == []


def _kml_post(api, user, data, dry_run, filename="farm.kml"):
    return api.post(
        "/farm-map/sectors/import-kml",
        files={"file": (filename, data, "application/vnd.google-earth.kml+xml")},
        data={"dry_run": "true" if dry_run else "false"},
        headers=_auth(user),
    )


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_kml_import_dry_run_then_create(api, owner):
    _sector(api, owner, name="Bloc A")  # makes "Bloc A" a duplicate
    res = _kml_post(api, owner, KML, dry_run=True)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [s["name"] for s in body["sectors"]] == ["Bloc B"]
    assert body["sectors"][0]["id"] is None and body["sectors"][0]["area_ha"] > 0
    assert {(s["name"], s["reason"]) for s in body["skipped"]} == {
        ("Bloc A", "duplicate_name"),
        ("Puits", "not_a_polygon"),
        ("Cassé", "invalid_geometry"),
    }
    assert len(api.get("/sectors", headers=_auth(owner)).json()) == 1  # nothing written

    res = _kml_post(api, owner, KML, dry_run=False)
    assert res.status_code == 201, res.text
    new_id = res.json()["sectors"][0]["id"]
    assert isinstance(new_id, int)
    revs = api.get(f"/sectors/{new_id}/geometry-history", headers=_auth(owner)).json()
    assert [r["source"] for r in revs["revisions"]] == ["kml"]

    assert _kml_post(api, owner, b"<gpx/>", dry_run=True).status_code == 400


def _zone(owner, sector_id, **kw):
    from apps.irrigation.models import Zone

    fields = dict(
        user=owner,
        name="Z1",
        space=2000.0,
        critical_moisture_threshold=25.0,
        pomp_flow_rate=5.0,
        elevation_m=120.0,
        soil_param_TAW=81.0,
        soil_param_RAW=40.5,
        irrigation_water_quantity=500_000.0,  # L/day cap: never binding here
        sector_id=sector_id,
    )
    fields.update(kw)
    return Zone.objects.create(**fields)


def _weather(et0=5.0, rain_future=0.0):
    def fake(*, start, end, latitude, longitude):
        out = {}
        day = start
        today = dt.datetime.now(dt.UTC).date()
        while day <= end:
            out[day.isoformat()] = {
                "et0_mm": et0,
                "rain_mm": rain_future if day > today else 0.0,
            }
            day += dt.timedelta(days=1)
        return out

    return fake


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_insights_water_balance_and_recommendation(api, owner, other, monkeypatch):
    monkeypatch.setattr(insights_mod, "fetch_openmeteo_daily_water", _weather(et0=5.0))
    sid = _sector(api, owner)

    # No zone yet: balance unavailable, the vegetation reading still answers.
    body = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()
    assert body["water_balance"] == {"available": False, "reason": "no_zone"}
    assert body["recommendation"] is None
    assert body["vegetation"]["status"] == "insufficient_data"

    zone = _zone(owner, sid)
    body = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()
    wb = body["water_balance"]
    # 14 days × 5 mm × Kc 1.0 = 70 mm, under TAW 81 → Dr = 70 ≥ RAW 40.5.
    assert wb["available"] is True and wb["dr_mm"] == 70.0
    assert wb["status"] == "irrigate_now" and len(wb["days"]) == 14
    assert wb["depletion_pct"] == pytest.approx(86.4, abs=0.1)
    assert body["kc"] == {"value": 1.0, "source": "default", "period_name": None}
    rec = body["recommendation"]
    assert (
        rec["action"] == "irrigate" and rec["volume_m3"] > 0 and rec["duration_min"] > 0
    )
    assert body["zone_ids"] == [zone.id]

    # The farmer's Kc table halves crop use → 35 mm, "irrigate soon".
    today = dt.datetime.now(dt.UTC).date()
    res = api.post(
        "/kc",
        json={
            "name": "Tomate",
            "plant_name": "Tomate",
            "zone_id": zone.id,
            "periods": [
                {
                    "period_name": "Initial",
                    "start_date": (today - dt.timedelta(days=60)).isoformat(),
                    "end_date": (today + dt.timedelta(days=60)).isoformat(),
                    "kc_value": 0.5,
                }
            ],
        },
        headers=_auth(owner),
    )
    assert res.status_code in (200, 201), res.text
    body = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()
    assert body["kc"] == {"value": 0.5, "source": "kc_period", "period_name": "Initial"}
    assert body["water_balance"]["dr_mm"] == 35.0
    assert body["water_balance"]["status"] == "irrigate_soon"
    assert body["recommendation"]["action"] == "none"

    # Rain coming → wait.
    monkeypatch.setattr(
        insights_mod, "fetch_openmeteo_daily_water", _weather(et0=5.0, rain_future=6.0)
    )
    body = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()
    assert body["recommendation"]["action"] == "wait"

    assert (
        api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(other)).status_code
        == 404
    )


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_insights_counts_metered_irrigation_and_reads_ndvi(api, owner, monkeypatch):
    from apps.sensors.models import WaterFlowSensor

    monkeypatch.setattr(insights_mod, "fetch_openmeteo_daily_water", _weather(et0=5.0))
    monkeypatch.setattr(ndvi_router, "get_client", lambda: _FakeCDSE())
    sid = _sector(api, owner)
    zone = _zone(owner, sid)
    base = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()

    # 2 h at 20 m³/h today = 40 m³ = 40 000 L over the sector → net depth.
    now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    hour = now if now.hour >= 2 else now + dt.timedelta(hours=2)
    for h in (hour - dt.timedelta(hours=1), hour - dt.timedelta(hours=2)):
        for minute in (5, 35):
            WaterFlowSensor.objects.create(
                zone=zone,
                user=owner,
                value=20.0,
                timestamp=h + dt.timedelta(minutes=minute),
            )
    body = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()
    assert body["water_balance"]["days"][-1]["irrigation_mm"] > 0
    assert body["water_balance"]["dr_mm"] < base["water_balance"]["dr_mm"]

    # Stored NDVI (declining) + a dry balance → water stress.
    api.get(f"/farm-map/ndvi/sectors/{sid}/stats", headers=_auth(owner))
    veg = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()[
        "vegetation"
    ]
    assert veg["ndvi"]["mean"] == 0.48 and veg["ndvi"]["trend_per_week"] < 0
    assert veg["status"] in ("water_stress", "declining")


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_forecast_layer_lists_drawn_sectors(api, owner, monkeypatch):
    monkeypatch.setenv("ET_FORECAST_PROVIDER", "mock")
    sid = _sector(api, owner)
    api.post("/sectors", json={"name": "Sans contour"}, headers=_auth(owner))
    body = api.get("/farm-map/forecast?days=3", headers=_auth(owner)).json()
    assert body["provider"] == "mock" and len(body["days"]) == 3
    assert [s["sector_id"] for s in body["sectors"]] == [sid]
    s = body["sectors"][0]
    assert len(s["daily"]) == 3 and s["centroid"]["lat"] == pytest.approx(
        32.89, abs=0.01
    )
    assert s["balance_mm"] == pytest.approx(
        s["total_rain_mm"] - s["total_et0_mm"], abs=0.11
    )
    assert api.get("/farm-map/forecast?days=9", headers=_auth(owner)).status_code == 422


# ------------------------------------------------------- insights language
_ARABIC = re.compile(r"[؀-ۿ]")
_FRENCH_ACCENTS = re.compile(r"[àâçéèêëîïôûùüÿœ]", re.IGNORECASE)


def _strings(body):
    """The four human strings the ``lang`` parameter translates."""
    return [
        body["recommendation"]["message"],
        body["vegetation"]["label"],
        body["vegetation"]["message"],
        body["water_balance"]["assumption"],
    ]


def _codes(body):
    rec = body["recommendation"]
    return (
        rec["action"],
        rec["reason"],
        rec["volume_m3"],
        body["vegetation"]["status"],
        body["water_balance"]["status"],
        body["water_balance"]["dr_mm"],
    )


@pytest.mark.parametrize(
    ("requested", "preferred", "expected"),
    [
        ("en", "fr", "en"),
        ("ar", "fr", "ar"),
        ("fr", "ar", "fr"),
        ("EN", "fr", "en"),
        (None, "ar", "ar"),
        (None, "fr", "fr"),
        (None, None, "fr"),
        (None, "", "fr"),
        (None, "de", "fr"),
        ("", "ar", "ar"),
        ("de", "ar", "fr"),  # unknown ?lang= → fr, not the preference
    ],
)
def test_resolve_lang(requested, preferred, expected):
    assert insights_mod.resolve_lang(requested, preferred) == expected


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_insights_lang_param(api, owner, monkeypatch):
    monkeypatch.setattr(insights_mod, "fetch_openmeteo_daily_water", _weather(et0=5.0))
    sid = _sector(api, owner)
    _zone(owner, sid)
    url = f"/farm-map/sectors/{sid}/insights"

    fr = api.get(url, headers=_auth(owner)).json()
    assert fr["lang"] == "fr"
    assert fr["recommendation"]["message"].startswith("Irriguer ")
    assert fr["water_balance"]["assumption"].startswith(
        "Sol supposé à la capacité au champ le "
    )
    assert fr["vegetation"]["label"] == "Stress hydrique probable"

    en = api.get(f"{url}?lang=en", headers=_auth(owner)).json()
    assert en["lang"] == "en"
    assert en["recommendation"]["message"].startswith("Irrigate ")
    assert "readily available water (40.5 mm)" in en["recommendation"]["message"]
    assert en["vegetation"]["label"] == "Probable water stress"
    assert en["vegetation"]["message"].startswith("No recent satellite image")
    assert en["water_balance"]["assumption"].startswith(
        "Soil assumed at field capacity on "
    )
    assert "14-day balance" in en["water_balance"]["assumption"]
    for text in _strings(en):
        assert not _FRENCH_ACCENTS.search(text), text

    ar = api.get(f"{url}?lang=ar", headers=_auth(owner)).json()
    assert ar["lang"] == "ar"
    assert ar["recommendation"]["message"].startswith("يُنصح بالري: ")
    assert "(40.5 mm)" in ar["recommendation"]["message"]
    assert ar["vegetation"]["label"] == "إجهاد مائي محتمل"
    assert ar["water_balance"]["assumption"].startswith("يُفترض أن التربة")
    for text in _strings(ar):
        assert _ARABIC.search(text), text
        assert not _FRENCH_ACCENTS.search(text), text

    # Only the human strings move; codes and numbers do not.
    assert _codes(fr) == _codes(en) == _codes(ar)
    assert set(fr) == set(en) == set(ar)

    # An unknown language is French, not a 422.
    res = api.get(f"{url}?lang=de", headers=_auth(owner))
    assert res.status_code == 200
    assert res.json()["lang"] == "fr" and _strings(res.json()) == _strings(fr)


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_insights_lang_defaults_to_preferred_language(api, owner, monkeypatch):
    monkeypatch.setattr(insights_mod, "fetch_openmeteo_daily_water", _weather(et0=5.0))
    sid = _sector(api, owner)
    _zone(owner, sid)
    owner.preferred_language = "ar"
    owner.save(update_fields=["preferred_language"])
    url = f"/farm-map/sectors/{sid}/insights"

    body = api.get(url, headers=_auth(owner)).json()
    assert body["lang"] == "ar"
    assert body["recommendation"]["message"].startswith("يُنصح بالري: ")
    assert body["vegetation"]["label"] == "إجهاد مائي محتمل"

    # ?lang= overrides the preference; an unknown one falls back to fr.
    assert api.get(f"{url}?lang=en", headers=_auth(owner)).json()["lang"] == "en"
    body = api.get(f"{url}?lang=xx", headers=_auth(owner)).json()
    assert body["lang"] == "fr"
    assert body["recommendation"]["message"].startswith("Irriguer ")


class _FakeCDSEBareSoil(_FakeCDSE):
    """Freshly sown field: low, flat NDVI."""

    def ndvi_stats(self, geometry, start, end, kind="ndvi"):
        rows = super().ndvi_stats(geometry, start, end, kind)
        for row, mean in zip(rows, (0.19, 0.2, 0.2, 0.21), strict=True):
            row["mean"] = mean
        return rows


def _kc_initial(api, owner, zone_id, name="Initial", value=0.4):
    today = dt.datetime.now(dt.UTC).date()
    res = api.post(
        "/kc",
        json={
            "name": "Blé",
            "plant_name": "Blé",
            "zone_id": zone_id,
            "periods": [
                {
                    "period_name": name,
                    "start_date": (today - dt.timedelta(days=10)).isoformat(),
                    "end_date": (today + dt.timedelta(days=20)).isoformat(),
                    "kc_value": value,
                }
            ],
        },
        headers=_auth(owner),
    )
    assert res.status_code in (200, 201), res.text


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_stale_soil_probe_is_ignored(api, owner, monkeypatch):
    from apps.sensors.models import SoilMoistureMedium

    monkeypatch.setattr(insights_mod, "fetch_openmeteo_daily_water", _weather(et0=2.0))
    sid = _sector(api, owner)
    zone = _zone(owner, sid)
    now = dt.datetime.now(dt.UTC)
    SoilMoistureMedium.objects.create(
        zone=zone, user=owner, value=27.0, timestamp=now - dt.timedelta(days=150)
    )
    veg = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()[
        "vegetation"
    ]
    assert veg["soil_moisture_pct"] is None  # a June reading is not "the soil now"

    SoilMoistureMedium.objects.create(
        zone=zone, user=owner, value=33.0, timestamp=now - dt.timedelta(hours=2)
    )
    veg = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()[
        "vegetation"
    ]
    assert veg["soil_moisture_pct"] == 33.0


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_low_ndvi_at_initial_stage_reads_as_establishing(api, owner, monkeypatch):
    from apps.sensors.models import SoilMoistureMedium

    monkeypatch.setattr(insights_mod, "fetch_openmeteo_daily_water", _weather(et0=2.0))
    monkeypatch.setattr(ndvi_router, "get_client", lambda: _FakeCDSEBareSoil())
    sid = _sector(api, owner)
    zone = _zone(owner, sid)
    SoilMoistureMedium.objects.create(
        zone=zone, user=owner, value=34.0, timestamp=dt.datetime.now(dt.UTC)
    )
    api.get(f"/farm-map/ndvi/sectors/{sid}/stats", headers=_auth(owner))

    # No crop calendar: low NDVI on moist soil is "low vigour, other cause".
    veg = api.get(f"/farm-map/sectors/{sid}/insights", headers=_auth(owner)).json()[
        "vegetation"
    ]
    assert veg["status"] == "low_vigor_other"

    # The farmer's calendar says "Initial": the same NDVI is expected.
    _kc_initial(api, owner, zone.id)
    body = api.get(
        f"/farm-map/sectors/{sid}/insights?lang=fr", headers=_auth(owner)
    ).json()
    veg = body["vegetation"]
    assert body["kc"]["period_name"] == "Initial"
    assert veg["status"] == "healthy" and veg["label"] == "Culture en démarrage"
    assert "NDVI bas attendu" in veg["message"]
