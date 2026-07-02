"""fastapp error taxonomy + handlers — wire-compatible with the Django side.

The classes mirror ``agriapi/errors.py`` (same ``code`` strings, same
``http_status``) and the handlers reproduce ``agriapi/exception_handler.py``'s
response shape::

    {"error": {"code": "<machine-readable>", "message": "<human-readable>"}}

so a route moved from Django to fastapp keeps returning byte-identical error
envelopes and no client has to care which process answered. When the strangler
migration completes, this taxonomy is the survivor; until then any change here
must be applied to ``agriapi/errors.py`` too.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AgriError(Exception):
    """Base for all known Agrilogy business errors. Do not raise this
    directly — use one of the subclasses.
    """

    http_status: int = 500
    code: str = "agri_error"


class AgriNotFoundError(AgriError):
    """Requested resource does not exist or isn't accessible to the caller."""

    http_status = 404
    code = "not_found"


class AgriValidationError(AgriError):
    """Caller's input is malformed or violates a business invariant."""

    http_status = 400
    code = "validation_error"


class AgriForbiddenError(AgriError):
    """Authenticated caller is not authorized for this action."""

    http_status = 403
    code = "forbidden"


class AgriConflictError(AgriError):
    """The operation conflicts with the current state of the resource."""

    http_status = 409
    code = "conflict"


class AgriUnavailableError(AgriError):
    """An upstream dependency (DB, email, external API) is temporarily down."""

    http_status = 503
    code = "service_unavailable"


def agri_error_response(exc: AgriError) -> JSONResponse:
    """The exact JSON envelope agriapi.exception_handler produces."""
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": exc.code, "message": str(exc) or exc.code}},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the AgriError → JSON mapping to a FastAPI app.

    Anything that is NOT an AgriError falls through to FastAPI's defaults
    (HTTPException / RequestValidationError keep their stock behaviour),
    matching the Django handler's fall-through to DRF.
    """

    @app.exception_handler(AgriError)
    async def _handle_agri_error(request: Request, exc: AgriError) -> JSONResponse:
        return agri_error_response(exc)
