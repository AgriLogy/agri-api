"""F0 smoke tests: the sidecar boots, /healthz answers, and the error
handlers produce the exact `{"error": {"code", "message"}}` envelope the
Django side (agriapi.exception_handler) emits. No DB required.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fastapp.errors import (
    AgriConflictError,
    AgriError,
    AgriForbiddenError,
    AgriNotFoundError,
    AgriUnavailableError,
    AgriValidationError,
)
from fastapp.main import app, settings


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "fastapp"
    assert body["version"] == settings.version
    # semantic-release keeps a real version in pyproject; the 0.0.0 fallback
    # showing up here would mean the pyproject read regressed.
    assert body["version"] != "0.0.0"


def test_docs_mounted_at_fast_prefix(client):
    # New-path-only surface: docs live under /api/fast/ so they can never
    # collide with a Django route during the strangler migration.
    assert client.get("/api/fast/docs").status_code == 200
    assert client.get("/api/fast/openapi.json").status_code == 200


@pytest.mark.parametrize(
    ("exc_cls", "status", "code"),
    [
        (AgriNotFoundError, 404, "not_found"),
        (AgriValidationError, 400, "validation_error"),
        (AgriForbiddenError, 403, "forbidden"),
        (AgriConflictError, 409, "conflict"),
        (AgriUnavailableError, 503, "service_unavailable"),
        (AgriError, 500, "agri_error"),
    ],
)
def test_agri_error_handler_shape(client, exc_cls, status, code):
    """Raising an AgriError from a route yields the Django-identical envelope."""
    path = f"/_test/{code}"
    if not any(getattr(r, "path", None) == path for r in app.routes):

        def _make_boom(cls):
            def _boom():
                raise cls("kaboom")

            return _boom

        app.add_api_route(path, _make_boom(exc_cls), methods=["GET"])

    resp = client.get(path)
    assert resp.status_code == status
    assert resp.json() == {"error": {"code": code, "message": "kaboom"}}


def test_agri_error_empty_message_falls_back_to_code(client):
    """agriapi.exception_handler uses `str(exc) or exc.code`; parity here."""
    path = "/_test/empty-message"
    if not any(getattr(r, "path", None) == path for r in app.routes):

        @app.get(path)
        def _boom_empty():
            raise AgriNotFoundError()

    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "not_found"}}
