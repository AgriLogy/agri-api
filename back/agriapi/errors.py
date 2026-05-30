"""Agrilogy error taxonomy.

Business code raises subclasses of ``AgriError``; the DRF exception
handler in ``agriapi.exception_handler`` maps them to consistent
JSON responses of shape::

    {"error": {"code": "<machine-readable>", "message": "<human-readable>"}}

Why a hierarchy?
  * Each error carries its own ``http_status`` and ``code`` — callers
    can branch on ``code``, ops can grep logs by ``code``.
  * The handler is one place to translate domain → HTTP. No per-view
    try/except chains.
  * New domain errors subclass the closest abstract one. Anything not
    a subclass of ``AgriError`` is treated as an unexpected 500 by the
    handler.

Pattern adapted from ``revly-core``'s error hierarchy.
"""

from __future__ import annotations


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
