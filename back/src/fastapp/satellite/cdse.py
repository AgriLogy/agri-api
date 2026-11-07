"""Copernicus Data Space Ecosystem (CDSE) Sentinel Hub client for NDVI.

Server-side only: the OAuth client secret never reaches the browser. Three
calls, all on Sentinel-2 L2A (10 m):

- ``acquisitions``: Catalog API — dates with imagery over a sector, filtered
  by scene cloud cover;
- ``ndvi_png``: Process API — a colourised NDVI PNG clipped to the sector
  outline (transparent outside it, and on cloud / shadow / snow pixels);
- ``ndvi_stats``: Statistical API — per-date mean/min/max NDVI over the
  sector, counting only clear pixels.

Results are cached in Redis (images per date never change; date lists and
statistics are refreshed every few hours) to stay inside the free quota.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("fastapp.satellite.cdse")

TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/"
    "openid-connect/token"
)
SH_BASE = "https://sh.dataspace.copernicus.eu"
PROCESS_PATH = "/api/v1/process"
STATS_PATH = "/api/v1/statistics"
CATALOG_PATH = "/api/v1/catalog/1.0.0/search"
COLLECTION = "sentinel-2-l2a"
CRS84 = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"

# Normalised-difference indices: (a - b) / (a + b) over Sentinel-2 L2A bands.
# Red-edge (B05) and SWIR (B11) are 20 m bands, resampled to the 10 m grid.
INDEXES: dict[str, dict[str, Any]] = {
    "ndvi": {
        "bands": ("B08", "B04"),
        "use": "vegetation greenness and vigour",
        "classes": (0.3, 0.6),
        "legend": [
            (-0.2, "#a60026"),
            (0.0, "#d73027"),
            (0.2, "#fdae61"),
            (0.4, "#fee08c"),
            (0.6, "#a6d96a"),
            (0.8, "#1a9850"),
        ],
    },
    "ndmi": {
        "bands": ("B08", "B11"),
        "use": "canopy water content (water-stress support)",
        "classes": (0.0, 0.2),
        "legend": [
            (-0.4, "#8c510a"),
            (-0.2, "#d8b365"),
            (0.0, "#f6e8c3"),
            (0.2, "#c7eae5"),
            (0.4, "#5ab4ac"),
            (0.6, "#01665e"),
        ],
    },
    "ndre": {
        "bands": ("B08", "B05"),
        "use": "chlorophyll in dense canopy (red edge)",
        "classes": (0.2, 0.4),
        "legend": [
            (-0.1, "#a60026"),
            (0.1, "#fdae61"),
            (0.2, "#fee08c"),
            (0.3, "#d9ef8b"),
            (0.45, "#66bd63"),
            (0.6, "#006837"),
        ],
    },
}
# False colour (NIR, red, green): a picture, not an index — image only.
IMAGE_KINDS = (*INDEXES, "false_color")

# Scene classification codes treated as "not ground": cloud shadow, cloud
# medium/high probability, cirrus, snow. Index values there are meaningless.
_MASK_SCL = "[3, 8, 9, 10, 11]"


def _hex_rgb(h: str) -> str:
    return (
        "[" + ", ".join(f"{int(h[i : i + 2], 16) / 255:.3f}" for i in (1, 3, 5)) + "]"
    )


def image_evalscript(kind: str) -> str:
    if kind == "false_color":
        return f"""//VERSION=3
function setup() {{
  return {{ input: ["B03", "B04", "B08", "SCL", "dataMask"], output: {{ bands: 4 }} }};
}}
function evaluatePixel(s) {{
  if (!s.dataMask || {_MASK_SCL}.includes(s.SCL)) return [0, 0, 0, 0];
  return [2.5 * s.B08, 2.5 * s.B04, 2.5 * s.B03, 1];
}}
"""
    spec = INDEXES[kind]
    a, b = spec["bands"]
    stops = ", ".join(f"{v}" for v, _ in spec["legend"])
    colours = ", ".join(_hex_rgb(c) for _, c in spec["legend"])
    return f"""//VERSION=3
function setup() {{
  return {{ input: ["{a}", "{b}", "SCL", "dataMask"], output: {{ bands: 4 }} }};
}}
function evaluatePixel(s) {{
  if (!s.dataMask || {_MASK_SCL}.includes(s.SCL)) return [0, 0, 0, 0];
  const v = (s.{a} - s.{b}) / (s.{a} + s.{b});
  const c = colorBlend(v, [{stops}], [{colours}]);
  return [c[0], c[1], c[2], 1];
}}
"""


def stats_evalscript(kind: str) -> str:
    """Index value + 3 class indicators. The Statistical API's mean of an
    indicator band over clear pixels is the share of the sector in that class,
    so area classes cost no extra request."""
    spec = INDEXES[kind]
    a, b = spec["bands"]
    lo, hi = spec["classes"]
    return f"""//VERSION=3
function setup() {{
  return {{
    input: [{{ bands: ["{a}", "{b}", "SCL", "dataMask"] }}],
    output: [
      {{ id: "index", bands: 1, sampleType: "FLOAT32" }},
      {{ id: "classes", bands: 3, sampleType: "FLOAT32" }},
      {{ id: "dataMask", bands: 1 }}
    ]
  }};
}}
function evaluatePixel(s) {{
  const v = (s.{a} - s.{b}) / (s.{a} + s.{b});
  const clear = s.dataMask === 1 && !{_MASK_SCL}.includes(s.SCL);
  return {{
    index: [v],
    classes: [v < {lo} ? 1 : 0, v >= {lo} && v < {hi} ? 1 : 0, v >= {hi} ? 1 : 0],
    dataMask: [clear ? 1 : 0]
  }};
}}
"""


def legend(kind: str) -> list[dict[str, Any]]:
    return [{"value": v, "color": c} for v, c in INDEXES[kind]["legend"]]


# Back-compat names (NDVI was the first index).
NDVI_IMAGE_EVALSCRIPT = image_evalscript("ndvi")
NDVI_STATS_EVALSCRIPT = stats_evalscript("ndvi")
NDVI_LEGEND = legend("ndvi")


class SatelliteError(Exception):
    """kind: not_configured | auth | rate_limit | unavailable | bad_request"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def bbox_of(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) of a Polygon/MultiPolygon."""
    coords: list[list[float]] = []
    if geometry.get("type") == "Polygon":
        for ring in geometry.get("coordinates") or []:
            coords.extend(ring)
    elif geometry.get("type") == "MultiPolygon":
        for poly in geometry.get("coordinates") or []:
            for ring in poly:
                coords.extend(ring)
    pts = [
        (float(p[0]), float(p[1]))
        for p in coords
        if isinstance(p, (list, tuple)) and len(p) >= 2
    ]
    if len(pts) < 3:
        raise SatelliteError("bad_request", "The sector has no usable outline.")
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def image_size(
    geometry: dict[str, Any], metres_per_px: float = 10.0
) -> tuple[int, int]:
    """Pixel size for ~10 m/px over the sector bbox, clamped to 16..1024."""
    min_lon, min_lat, max_lon, max_lat = bbox_of(geometry)
    lat0 = math.radians((min_lat + max_lat) / 2)
    w_m = (max_lon - min_lon) * 111_320 * math.cos(lat0)
    h_m = (max_lat - min_lat) * 110_540
    w = max(16, min(1024, round(w_m / metres_per_px)))
    h = max(16, min(1024, round(h_m / metres_per_px)))
    return int(w), int(h)


def geometry_key(geometry: dict[str, Any]) -> str:
    return hashlib.sha1(
        json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


class _Cache:
    """Redis with a silent no-op fallback (a cache miss never breaks a call)."""

    def __init__(self, url: str | None):
        self._url = url
        self._client = None

    def _redis(self):
        if not self._url:
            return None
        if self._client is None:
            import redis

            self._client = redis.from_url(self._url)
        return self._client

    def get(self, key: str) -> bytes | None:
        try:
            r = self._redis()
            return r.get(key) if r else None
        except Exception:  # pragma: no cover - Redis down → just recompute
            return None

    def set(self, key: str, value: bytes, ttl: int) -> None:
        try:
            r = self._redis()
            if r:
                r.set(key, value, ex=ttl)
        except Exception:  # pragma: no cover
            pass


class CDSEClient:
    DATES_TTL = 6 * 3600
    STATS_TTL = 6 * 3600
    IMAGE_TTL = 30 * 86400

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.Client | None = None,
        cache_url: str | None = None,
        timeout: float = 60.0,
    ):
        if not client_id or not client_secret:
            raise SatelliteError(
                "not_configured", "Satellite imagery is not configured."
            )
        self._id = client_id
        self._secret = client_secret
        self._http = http or httpx.Client(
            timeout=timeout, headers={"User-Agent": "agri-api-ndvi/1.0"}
        )
        self._cache = _Cache(cache_url)
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = threading.Lock()

    # -- auth ---------------------------------------------------------------
    def _bearer(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            try:
                resp = self._http.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._id,
                        "client_secret": self._secret,
                    },
                )
            except httpx.HTTPError as exc:
                raise SatelliteError(
                    "unavailable", "Copernicus login is unreachable."
                ) from exc
            if resp.status_code in (400, 401, 403):
                raise SatelliteError("auth", "Copernicus rejected the credentials.")
            if resp.status_code == 429:
                raise SatelliteError("rate_limit", "Copernicus login rate limit.")
            if resp.status_code >= 300:
                raise SatelliteError(
                    "unavailable", f"Copernicus login error {resp.status_code}."
                )
            body = resp.json()
            self._token = body["access_token"]
            self._token_exp = time.time() + float(body.get("expires_in") or 300)
            return self._token

    def _post(self, path: str, payload: dict[str, Any], accept: str) -> httpx.Response:
        for attempt in (1, 2):
            try:
                resp = self._http.post(
                    SH_BASE + path,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._bearer()}",
                        "Accept": accept,
                    },
                )
            except httpx.TimeoutException as exc:
                raise SatelliteError("unavailable", "Copernicus timed out.") from exc
            except httpx.HTTPError as exc:
                raise SatelliteError(
                    "unavailable", "Copernicus is unreachable."
                ) from exc
            if resp.status_code == 401 and attempt == 1:
                self._token = None  # expired early: log in again once
                continue
            break
        if resp.status_code == 429:
            raise SatelliteError(
                "rate_limit", "Copernicus quota or rate limit reached."
            )
        if resp.status_code in (401, 403):
            raise SatelliteError("auth", "Copernicus rejected the request.")
        if resp.status_code >= 500:
            raise SatelliteError("unavailable", f"Copernicus error {resp.status_code}.")
        if resp.status_code >= 400:
            log.warning("cdse %s %s: %s", path, resp.status_code, resp.text[:500])
            raise SatelliteError("bad_request", "Copernicus refused the request.")
        return resp

    # -- API ----------------------------------------------------------------
    def acquisitions(
        self,
        geometry: dict[str, Any],
        start: dt.date,
        end: dt.date,
        max_cloud: int,
    ) -> list[dict[str, Any]]:
        """Distinct acquisition dates (newest first) with scene cloud cover."""
        key = f"ndvi:dates:{geometry_key(geometry)}:{start}:{end}:{max_cloud}"
        if (hit := self._cache.get(key)) is not None:
            return json.loads(hit)
        payload: dict[str, Any] = {
            "bbox": list(bbox_of(geometry)),
            "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
            "collections": [COLLECTION],
            "limit": 100,
            "filter": f"eo:cloud_cover <= {int(max_cloud)}",
            "filter-lang": "cql2-text",
            "fields": {
                "include": ["properties.datetime", "properties.eo:cloud_cover"],
                "exclude": [],
            },
        }
        by_date: dict[str, float] = {}
        for _page in range(10):
            body = self._post(CATALOG_PATH, payload, "application/geo+json").json()
            for feat in body.get("features") or []:
                props = feat.get("properties") or {}
                when = str(props.get("datetime") or "")[:10]
                cloud = props.get("eo:cloud_cover")
                if not when:
                    continue
                cloud_f = float(cloud) if isinstance(cloud, (int, float)) else 100.0
                by_date[when] = min(by_date.get(when, 100.0), cloud_f)
            nxt = (body.get("context") or {}).get("next")
            if not nxt:
                break
            payload["next"] = nxt
        out = [
            {"date": d, "cloud_cover": round(c, 1)}
            for d, c in sorted(by_date.items(), reverse=True)
        ]
        self._cache.set(key, json.dumps(out).encode(), self.DATES_TTL)
        return out

    def ndvi_png(
        self, geometry: dict[str, Any], day: dt.date, kind: str = "ndvi"
    ) -> bytes:
        if kind not in IMAGE_KINDS:
            raise SatelliteError("bad_request", f"Unknown image type {kind!r}.")
        key = f"sat:png:{kind}:{geometry_key(geometry)}:{day}"
        if (hit := self._cache.get(key)) is not None:
            return hit
        w, h = image_size(geometry)
        payload = {
            "input": {
                "bounds": {"geometry": geometry, "properties": {"crs": CRS84}},
                "data": [
                    {
                        "type": COLLECTION,
                        "dataFilter": {
                            "timeRange": {
                                "from": f"{day.isoformat()}T00:00:00Z",
                                "to": f"{day.isoformat()}T23:59:59Z",
                            },
                            "mosaickingOrder": "leastCC",
                        },
                    }
                ],
            },
            "output": {
                "width": w,
                "height": h,
                "responses": [
                    {"identifier": "default", "format": {"type": "image/png"}}
                ],
            },
            "evalscript": image_evalscript(kind),
        }
        png = self._post(PROCESS_PATH, payload, "image/png").content
        self._cache.set(key, png, self.IMAGE_TTL)
        return png

    def ndvi_stats(
        self,
        geometry: dict[str, Any],
        start: dt.date,
        end: dt.date,
        kind: str = "ndvi",
    ) -> list[dict[str, Any]]:
        """Per-day NDVI over clear pixels, oldest first; days without any
        clear pixel are dropped."""
        if kind not in INDEXES:
            raise SatelliteError("bad_request", f"Unknown index {kind!r}.")
        key = f"sat:stats:{kind}:{geometry_key(geometry)}:{start}:{end}"
        if (hit := self._cache.get(key)) is not None:
            return json.loads(hit)
        payload = {
            "input": {
                "bounds": {"geometry": geometry, "properties": {"crs": CRS84}},
                "data": [
                    {"type": COLLECTION, "dataFilter": {"mosaickingOrder": "leastCC"}}
                ],
            },
            "aggregation": {
                "timeRange": {
                    "from": f"{start.isoformat()}T00:00:00Z",
                    "to": f"{(end + dt.timedelta(days=1)).isoformat()}T00:00:00Z",
                },
                "aggregationInterval": {"of": "P1D"},
                "evalscript": stats_evalscript(kind),
                # ~10 m in degrees (the request CRS is lon/lat).
                "resx": 0.0001,
                "resy": 0.0001,
            },
        }
        body = self._post(STATS_PATH, payload, "application/json").json()
        out = parse_stats(body)
        self._cache.set(key, json.dumps(out).encode(), self.STATS_TTL)
        return out


def parse_stats(body: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in body.get("data") or []:
        if item.get("error"):
            continue
        outputs = item.get("outputs") or {}
        stats = (
            (outputs.get("index") or outputs.get("ndvi") or {})
            .get("bands", {})
            .get("B0", {})
            .get("stats", {})
        )
        cls_bands = (outputs.get("classes") or {}).get("bands") or {}
        total = stats.get("sampleCount") or 0
        nodata = stats.get("noDataCount") or 0
        valid = total - nodata
        mean = stats.get("mean")
        if not total or valid <= 0 or not isinstance(mean, (int, float)):
            continue
        if isinstance(mean, float) and math.isnan(mean):
            continue
        out.append(
            {
                "date": str((item.get("interval") or {}).get("from", ""))[:10],
                "mean": round(float(mean), 3),
                "min": _r(stats.get("min")),
                "max": _r(stats.get("max")),
                "std": _r(stats.get("stDev")),
                "valid_fraction": round(valid / total, 2),
                "classes": _classes(cls_bands),
            }
        )
    out.sort(key=lambda r: r["date"])
    return out


def _classes(bands: dict[str, Any]) -> dict[str, float] | None:
    """Share of clear pixels in low / medium / high (means of 0/1 bands)."""
    out: dict[str, float] = {}
    for name, band in (("low", "B0"), ("medium", "B1"), ("high", "B2")):
        mean = ((bands.get(band) or {}).get("stats") or {}).get("mean")
        if not isinstance(mean, (int, float)) or math.isnan(float(mean)):
            return None
        out[name] = round(float(mean), 3)
    return out


def _r(v: Any) -> float | None:
    return round(float(v), 3) if isinstance(v, (int, float)) else None
