"""Tests for the AgriError hierarchy + DRF exception handler.

Run with::

    DJANGO_ENV=test uv run pytest back/agriBack/tests_errors.py
"""
from __future__ import annotations

from rest_framework.test import APIRequestFactory

from agriBack.errors import (
    AgriConflictError,
    AgriError,
    AgriForbiddenError,
    AgriNotFoundError,
    AgriUnavailableError,
    AgriValidationError,
)
from agriBack.exception_handler import agri_exception_handler


def _context() -> dict:
    """Minimal DRF context dict — handler only reads `request` for the
    fallback path."""
    return {"request": APIRequestFactory().get("/")}


def test_agri_error_base_maps_to_500() -> None:
    resp = agri_exception_handler(AgriError("base"), _context())
    assert resp is not None
    assert resp.status_code == 500
    assert resp.data == {"error": {"code": "agri_error", "message": "base"}}


def test_not_found_maps_to_404() -> None:
    resp = agri_exception_handler(
        AgriNotFoundError("sensor 42 not found"), _context()
    )
    assert resp is not None
    assert resp.status_code == 404
    assert resp.data == {
        "error": {"code": "not_found", "message": "sensor 42 not found"}
    }


def test_validation_maps_to_400() -> None:
    resp = agri_exception_handler(
        AgriValidationError("hours must be > 0"), _context()
    )
    assert resp is not None
    assert resp.status_code == 400
    assert resp.data["error"]["code"] == "validation_error"


def test_forbidden_maps_to_403() -> None:
    resp = agri_exception_handler(AgriForbiddenError("nope"), _context())
    assert resp is not None
    assert resp.status_code == 403
    assert resp.data["error"]["code"] == "forbidden"


def test_conflict_maps_to_409() -> None:
    resp = agri_exception_handler(AgriConflictError("dup"), _context())
    assert resp is not None
    assert resp.status_code == 409
    assert resp.data["error"]["code"] == "conflict"


def test_unavailable_maps_to_503() -> None:
    resp = agri_exception_handler(
        AgriUnavailableError("supabase down"), _context()
    )
    assert resp is not None
    assert resp.status_code == 503
    assert resp.data["error"]["code"] == "service_unavailable"


def test_empty_message_falls_back_to_code() -> None:
    """str(exc) is empty → message == exc.code so callers never see empty strings."""
    resp = agri_exception_handler(AgriNotFoundError(), _context())
    assert resp is not None
    assert resp.data["error"]["message"] == "not_found"


def test_non_agri_error_falls_through() -> None:
    """Unknown exceptions fall through to DRF's default handler.

    The default returns None for non-DRF exceptions like RuntimeError.
    """
    resp = agri_exception_handler(RuntimeError("not ours"), _context())
    assert resp is None


def test_subclass_inherits_http_status_and_code() -> None:
    """Custom subclasses inherit parent status/code unless overridden."""

    class SensorNotFoundError(AgriNotFoundError):
        code = "sensor_not_found"

    resp = agri_exception_handler(
        SensorNotFoundError("sensor 1 not found"), _context()
    )
    assert resp is not None
    assert resp.status_code == 404  # inherited
    assert resp.data["error"]["code"] == "sensor_not_found"  # overridden
