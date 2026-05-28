"""DRF exception handler that maps ``AgriError`` subclasses to a
consistent JSON shape.

Wired in ``agriBack.settings.base`` via::

    REST_FRAMEWORK = {
        ...
        "EXCEPTION_HANDLER": "agriBack.exception_handler.agri_exception_handler",
    }

Behaviour:
  * If the exception is an ``AgriError`` subclass, return
    ``{"error": {"code": exc.code, "message": str(exc)}}`` with
    ``exc.http_status``.
  * Otherwise fall through to DRF's default handler so existing
    ValidationError/PermissionDenied/etc. responses are preserved.
"""
from __future__ import annotations

from typing import Any

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_exception_handler

from agriBack.errors import AgriError


def agri_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, AgriError):
        return Response(
            {"error": {"code": exc.code, "message": str(exc) or exc.code}},
            status=exc.http_status,
        )
    return drf_default_exception_handler(exc, context)
