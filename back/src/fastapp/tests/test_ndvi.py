"""NDVI from Copernicus Sentinel-2 (agri-api#459 phase 2).

The CDSE client is exercised against an httpx.MockTransport (no network);
the router tests need Postgres (sectors are written through the API and read
back through the Core mirror) and swap the client for a fake.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.routers import ndvi as ndvi_router
from fastapp.satellite import cdse
from fastapp.satellite.cdse import CDSEClient, SatelliteError, parse_stats
from fastapp.schema_compat import reset_sector_geometry_cache
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


def _client(handler) -> tuple[CDSEClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "identity.dataspace.copernicus.eu":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(wrapped))
    return CDSEClient("id", "secret", http=http), seen


# --------------------------------------------------------------------- unit
def test_requires_credentials():
    with pytest.raises(SatelliteError) as exc:
        CDSEClient("", "")
    assert exc.value.kind == "not_configured"


def test_image_size_is_about_10m_per_pixel_and_clamped():
    w, h = cdse.image_size(SQUARE)
    assert 15 <= w <= 25 and 18 <= h <= 26  # ~187 m x 221 m
    huge = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }
    assert cdse.image_size(huge) == (1024, 1024)


def test_bbox_rejects_empty_outline():
    with pytest.raises(SatelliteError):
        cdse.bbox_of({"type": "Polygon", "coordinates": [[[0, 0], None]]})


def test_acquisitions_dedupe_per_day_keep_clearest_and_page():
    pages = [
        {
            "features": [
                {
                    "properties": {
                        "datetime": "2026-10-01T10:30:00Z",
                        "eo:cloud_cover": 30,
                    }
                },
                {
                    "properties": {
                        "datetime": "2026-10-01T10:30:05Z",
                        "eo:cloud_cover": 5.04,
                    }
                },
            ],
            "context": {"next": 2},
        },
        {
            "features": [
                {
                    "properties": {
                        "datetime": "2026-10-06T10:30:00Z",
                        "eo:cloud_cover": 12,
                    }
                }
            ]
        },
    ]

    def handler(request):
        assert request.url.path == cdse.CATALOG_PATH
        body = json.loads(request.content)
        assert body["collections"] == ["sentinel-2-l2a"]
        assert body["filter"] == "eo:cloud_cover <= 40"
        return httpx.Response(200, json=pages.pop(0))

    client, seen = _client(handler)
    out = client.acquisitions(SQUARE, dt.date(2026, 9, 1), dt.date(2026, 10, 31), 40)
    assert out == [
        {"date": "2026-10-06", "cloud_cover": 12.0},
        {"date": "2026-10-01", "cloud_cover": 5.0},
    ]
    assert json.loads(seen[-1].content)["next"] == 2
    assert seen[1].headers["Authorization"] == "Bearer tok"


def test_png_request_is_clipped_to_the_sector_for_one_day():
    def handler(request):
        assert request.url.path == cdse.PROCESS_PATH
        body = json.loads(request.content)
        assert body["input"]["bounds"]["geometry"] == SQUARE
        rng = body["input"]["data"][0]["dataFilter"]["timeRange"]
        assert rng == {"from": "2026-10-06T00:00:00Z", "to": "2026-10-06T23:59:59Z"}
        assert "colorBlend" in body["evalscript"]
        return httpx.Response(200, content=b"\x89PNG...")

    client, _ = _client(handler)
    assert client.ndvi_png(SQUARE, dt.date(2026, 10, 6)) == b"\x89PNG..."


@pytest.mark.parametrize(
    "status,kind",
    [(429, "rate_limit"), (403, "auth"), (500, "unavailable"), (400, "bad_request")],
)
def test_error_mapping(status, kind):
    client, _ = _client(lambda r: httpx.Response(status, text="no"))
    with pytest.raises(SatelliteError) as exc:
        client.ndvi_png(SQUARE, dt.date(2026, 10, 6))
    assert exc.value.kind == kind


def test_expired_token_is_refreshed_once():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401 if calls["n"] == 1 else 200, content=b"png")

    client, seen = _client(handler)
    assert client.ndvi_png(SQUARE, dt.date(2026, 10, 6)) == b"png"
    assert sum(r.url.host.startswith("identity") for r in seen) == 2


def test_parse_stats_drops_cloudy_and_failed_days():
    body = {
        "data": [
            {
                "interval": {"from": "2026-10-06T00:00:00Z"},
                "outputs": {
                    "ndvi": {
                        "bands": {
                            "B0": {
                                "stats": {
                                    "min": 0.1,
                                    "max": 0.81234,
                                    "mean": 0.55555,
                                    "stDev": 0.1,
                                    "sampleCount": 400,
                                    "noDataCount": 100,
                                }
                            }
                        }
                    }
                },
            },
            {
                "interval": {"from": "2026-10-01T00:00:00Z"},
                "outputs": {
                    "ndvi": {
                        "bands": {
                            "B0": {
                                "stats": {
                                    "mean": "NaN",
                                    "sampleCount": 400,
                                    "noDataCount": 400,
                                }
                            }
                        }
                    }
                },
            },
            {
                "interval": {"from": "2026-10-03T00:00:00Z"},
                "error": {"type": "EXECUTION_ERROR"},
            },
        ]
    }
    assert parse_stats(body) == [
        {
            "date": "2026-10-06",
            "mean": 0.556,
            "min": 0.1,
            "max": 0.812,
            "std": 0.1,
            "valid_fraction": 0.75,
            "classes": None,
        }
    ]


def test_parse_stats_reads_index_output_and_area_classes():
    def band(mean):
        return {"stats": {"mean": mean}}

    body = {
        "data": [
            {
                "interval": {"from": "2026-10-06T00:00:00Z"},
                "outputs": {
                    "index": {
                        "bands": {
                            "B0": {
                                "stats": {
                                    "mean": 0.12,
                                    "min": -0.1,
                                    "max": 0.3,
                                    "stDev": 0.05,
                                    "sampleCount": 100,
                                    "noDataCount": 0,
                                }
                            }
                        }
                    },
                    "classes": {
                        "bands": {"B0": band(0.2), "B1": band(0.5), "B2": band(0.3)}
                    },
                },
            }
        ]
    }
    out = parse_stats(body)[0]
    assert out["mean"] == 0.12
    assert out["classes"] == {"low": 0.2, "medium": 0.5, "high": 0.3}


@pytest.mark.parametrize(
    "kind,bands",
    [("ndvi", "B08 - s.B04"), ("ndmi", "B08 - s.B11"), ("ndre", "B08 - s.B05")],
)
def test_index_evalscripts_use_the_right_bands(kind, bands):
    assert bands in cdse.image_evalscript(kind)
    script = cdse.stats_evalscript(kind)
    assert bands in script and 'id: "classes", bands: 3' in script
    lo, hi = cdse.INDEXES[kind]["classes"]
    assert f"v < {lo}" in script and f"v >= {hi}" in script


def test_false_colour_is_image_only():
    assert "2.5 * s.B08" in cdse.image_evalscript("false_color")
    client, _ = _client(lambda r: httpx.Response(200, content=b"x"))
    with pytest.raises(SatelliteError):
        client.ndvi_stats(
            SQUARE, dt.date(2026, 9, 1), dt.date(2026, 10, 1), "false_color"
        )
    with pytest.raises(SatelliteError):
        client.ndvi_png(SQUARE, dt.date(2026, 10, 6), "evi")


# ------------------------------------------------------------------- router
_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM: fastapp reads the test DB Django writes (needs Postgres)",
)


class _FakeClient:
    def acquisitions(self, geometry, start, end, max_cloud):
        assert geometry == SQUARE
        return [{"date": "2026-10-06", "cloud_cover": 3.0}]

    def ndvi_png(self, geometry, day, kind="ndvi"):
        assert kind in ("ndvi", "ndmi", "ndre", "false_color")
        return b"\x89PNG-fake"

    def ndvi_stats(self, geometry, start, end, kind="ndvi"):
        return [
            {
                "date": "2026-10-06",
                "mean": 0.6,
                "min": 0.2,
                "max": 0.8,
                "std": 0.1,
                "valid_fraction": 0.9,
            }
        ]


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)
    reset_sector_geometry_cache()
    return TestClient(app)


def _user(django_user_model, set_access_level, name):
    u = django_user_model.objects.create_user(
        username=name, email=f"{name}@example.com", password="x-29481"
    )
    set_access_level(u, "editor")
    return u


def _auth(u):
    return {"Authorization": f"Bearer {AccessToken.for_user(u)}"}


@_requires_pg
@pytest.mark.django_db(transaction=True)
def test_ndvi_endpoints(api, monkeypatch, django_user_model, set_access_level):
    owner = _user(django_user_model, set_access_level, "ndvi-owner")
    other = _user(django_user_model, set_access_level, "ndvi-other")
    res = api.post(
        "/sectors", json={"name": "S1", "geometry": SQUARE}, headers=_auth(owner)
    )
    assert res.status_code in (200, 201), res.text
    sid = res.json()["id"]

    # Not configured → 503 with a code the front can show.
    monkeypatch.setattr(get_settings(), "cdse_client_id", "")
    monkeypatch.setattr(ndvi_router, "_client", None)
    r = api.get(f"/farm-map/ndvi/sectors/{sid}/dates", headers=_auth(owner))
    assert r.status_code == 503 and r.json()["code"] == "not_configured"
    assert (
        api.get("/farm-map/ndvi/config", headers=_auth(owner)).json()["configured"]
        is False
    )

    monkeypatch.setattr(ndvi_router, "get_client", lambda: _FakeClient())
    r = api.get(f"/farm-map/ndvi/sectors/{sid}/dates", headers=_auth(owner))
    assert r.status_code == 200 and r.json()["dates"][0]["date"] == "2026-10-06"
    r = api.get(
        f"/farm-map/ndvi/sectors/{sid}/image?date=2026-10-06", headers=_auth(owner)
    )
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == b"\x89PNG-fake"
    r = api.get(f"/farm-map/ndvi/sectors/{sid}/stats", headers=_auth(owner))
    assert r.json()["series"][0]["mean"] == 0.6
    r = api.get(f"/farm-map/ndvi/sectors/{sid}/stats?index=ndmi", headers=_auth(owner))
    assert r.status_code == 200 and r.json()["index"] == "ndmi"
    assert (
        api.get(
            f"/farm-map/ndvi/sectors/{sid}/stats?index=false_color",
            headers=_auth(owner),
        ).status_code
        == 422
    )
    r = api.get(
        f"/farm-map/ndvi/sectors/{sid}/image?date=2026-10-06&index=false_color",
        headers=_auth(owner),
    )
    assert r.status_code == 200
    cfg = api.get("/farm-map/ndvi/config", headers=_auth(owner)).json()
    assert [i["id"] for i in cfg["indexes"]] == ["ndvi", "ndmi", "ndre"]
    assert "false_color" in cfg["image_kinds"]

    # Someone else's sector is invisible; so is an unauthenticated caller.
    assert (
        api.get(f"/farm-map/ndvi/sectors/{sid}/dates", headers=_auth(other)).status_code
        == 404
    )
    assert api.get(f"/farm-map/ndvi/sectors/{sid}/dates").status_code == 401
    assert (
        api.get(f"/farm-map/ndvi/sectors/{sid}/image", headers=_auth(owner)).status_code
        == 422
    )
