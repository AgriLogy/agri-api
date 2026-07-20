"""Structured logging for the fastapp processes (web sidecar, native Celery
worker/beat, MQTT subscriber) — stdlib only, no new dependency.

Why this exists
---------------
Every process in the image logged plain text through an unconfigured root
logger, so the ``extra={...}`` dicts call sites already pass (e.g.
``log.info("mqtt.weather.no_metrics", extra={"client": name})``) were
silently dropped. Promtail/Loki want one line = one JSON object with stable
keys it can index. This module renders exactly that.

Design
------
* ``JsonFormatter`` — emits one JSON object per record: ``ts`` (ISO-8601
  UTC), ``level``, ``logger``, ``msg``, ``request_id``, plus every non-standard
  attribute attached via ``extra=`` (so structured events just work), plus a
  rendered ``exc`` string when ``exc_info`` is set.
* ``request_id`` is carried in a :class:`contextvars.ContextVar` so an async
  request handler, and everything it awaits, share one id without threading it
  through call signatures. ``RequestIdFilter`` stamps it onto every record.
* ``configure_logging()`` is idempotent and dependency-free (takes plain
  args, never imports settings) so Django's ``LOGGING`` dict and Celery's
  ``setup_logging`` signal can both reuse the same ``JsonFormatter`` class.

Kept intentionally tiny and import-light: no third-party JSON logger, so it
adds nothing to the pinned dependency set and imports cleanly from the Django
process too (``fastapp.logging_config.JsonFormatter`` is on ``src/`` path).
"""

from __future__ import annotations

import datetime
import json
import logging
import sys
from contextvars import ContextVar

# Propagated across a request (and its awaited coroutines) by
# fastapp.middleware.RequestContextMiddleware; "-" when outside a request
# (Celery task, MQTT message, boot).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Standard LogRecord attributes — anything NOT in here was passed via
# ``extra=`` and is promoted to a top-level JSON key.
_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
        "request_id",
    }
)


class RequestIdFilter(logging.Filter):
    """Stamp the current request id onto every record so both the JSON and the
    text formatters can render it (and it's present even for library records
    that never saw our ``extra=``)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Referenced by name from Django's LOGGING dict
    (``"()": "fastapp.logging_config.JsonFormatter"``) as well as here."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.datetime.fromtimestamp(
            record.created, tz=datetime.timezone.utc
        ).isoformat(timespec="milliseconds")
        payload: dict[str, object] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        # Promote structured extras (skip Nones so keys stay stable/queryable).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_") and value is not None:
                payload[key] = _jsonable(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _jsonable(value: object) -> object:
    """Cheap guard so a stray non-serialisable extra never blows up a log
    call (logging must never raise). json.dumps' ``default=str`` covers the
    top level; this keeps nested containers sane too."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


_TEXT_FMT = "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s"

# Module-level guard so repeated imports / re-entry (uvicorn --workers respawn,
# a Celery signal firing twice) don't stack duplicate handlers on root.
_configured = False


def build_formatter(fmt: str) -> logging.Formatter:
    return JsonFormatter() if fmt.lower() == "json" else logging.Formatter(_TEXT_FMT)


def configure_logging(
    level: str = "INFO", fmt: str = "json", *, force: bool = False
) -> None:
    """Point the root logger at a single stdout handler using ``fmt``
    (``"json"`` | ``"text"``). Idempotent unless ``force=True``.

    Docker already sends stdout straight to the container log (json-file
    driver → Promtail), so one StreamHandler(sys.stdout) is all we need.
    """
    global _configured
    if _configured and not force:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(build_formatter(fmt))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Let uvicorn's access/error records flow through OUR root handler instead
    # of uvicorn's private plain-text ones, so they're JSON too. (The fast
    # entrypoint also passes --no-access-log; the middleware emits the
    # canonical structured access line.)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    # Trim the chattiest libraries so INFO stays signal, not noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _configured = True
