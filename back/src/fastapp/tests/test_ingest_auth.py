"""Shared-secret auth on the /ingest/* device webhooks.

With INGEST_TOKEN set, a request without the matching X-Ingest-Token header
is refused with a 401 before the body is even looked at. Unset, the routes
stay open (the pre-rollout behaviour). An empty JSON body is used throughout:
reaching validation (422/400) proves the request got past the auth check,
and no database is needed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fastapp.main import app
from fastapp.settings import get_settings

ROUTES = [
    "/ingest/bivocom",
    "/ingest/lorawan/chirpstack",
    "/ingest/weather",
    "/ingest/sensor",
]
TOKEN = "s3cret-ingest-token"


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def token_set(monkeypatch):
    monkeypatch.setattr(get_settings(), "ingest_token", TOKEN)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Ingest-Token": ""}, {"X-Ingest-Token": "wrong"}, {"Authorization": TOKEN}],
)
def test_missing_or_wrong_token_is_401(fast, token_set, route, headers):
    r = fast.post(route, content=b"[]", headers=headers)
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid ingest token."}


@pytest.mark.parametrize("route", ROUTES)
def test_right_token_passes_auth(fast, token_set, route):
    r = fast.post(route, content=b"[]", headers={"X-Ingest-Token": TOKEN})
    assert r.status_code in (400, 422), r.text


@pytest.mark.parametrize("route", ROUTES)
def test_unset_token_leaves_routes_open(fast, monkeypatch, route):
    monkeypatch.setattr(get_settings(), "ingest_token", "")
    r = fast.post(route, content=b"[]")
    assert r.status_code in (400, 422), r.text
