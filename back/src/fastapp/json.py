"""Django-style JSON rendering for byte-identical strangler parity.

Starlette's default ``JSONResponse`` serialises compact
(``separators=(",", ":")``, ``ensure_ascii=False``); django-ninja renders
with Python's ``json.dumps`` defaults (``", "`` / ``": "`` separators,
``ensure_ascii=True``). A route moved from Django to fastapp must produce the
SAME bytes — not just the same parsed object — so no proxy/log/length differs
across the cutover boundary. This module makes every fastapp response (and the
built-in error envelopes) match ninja's wire format.
"""

from __future__ import annotations

import json
import typing

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse


class DjangoStyleJSONResponse(JSONResponse):
    """JSONResponse whose bytes match django-ninja's default renderer:
    spaced separators + ascii-escaped non-ASCII, ``default=str`` for the odd
    Decimal/date that slips through (ninja's encoder does the same)."""

    def render(self, content: typing.Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=True,
            allow_nan=False,
            separators=(", ", ": "),
            default=str,
        ).encode("utf-8")


def register_django_style_json(app: FastAPI) -> None:
    """Route FastAPI's own HTTPException / validation error envelopes through
    the Django-style renderer too (``app.default_response_class`` only covers
    path-operation returns, not the framework's error handlers)."""

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        return DjangoStyleJSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        # django-ninja emits only type/loc/msg per error; pydantic v2 additionally
        # carries input/url/ctx. Strip those so the 422 body is byte-identical to
        # the Django surface being superseded.
        errors = [
            {k: v for k, v in err.items() if k not in ("input", "url", "ctx")}
            for err in exc.errors()
        ]
        return DjangoStyleJSONResponse({"detail": errors}, status_code=422)
