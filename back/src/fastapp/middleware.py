"""Request-scoped logging context for the fastapp sidecar.

A raw ASGI middleware (not BaseHTTPMiddleware, so it never buffers the
response body) that:

* adopts an inbound ``X-Request-ID`` or mints one, binds it to
  :data:`fastapp.logging_config.request_id_var` for the life of the request
  (so every log line the handler emits carries the same id), and echoes it
  back on the response — giving you one id to grep a whole request across
  app + access logs in Loki;
* emits exactly one structured access line per request (``event="http.access"``
  with method / path / status / duration_ms / client), which is why the
  entrypoint runs uvicorn with ``--no-access-log`` (no duplicate plain line).

The frequent, uninteresting liveness probe (``/healthz``, hit by the Docker
healthcheck every ~10s) is not access-logged, so it can't drown the signal.
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from fastapp.logging_config import request_id_var

logger = logging.getLogger("fastapp.access")

_REQUEST_ID_HEADER = b"x-request-id"
# Paths that must never produce an access line (health probes only).
_SILENT_PATHS = frozenset({"/healthz"})


class RequestContextMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        inbound = headers.get(_REQUEST_ID_HEADER)
        request_id = inbound.decode("latin-1") if inbound else uuid4().hex
        token = request_id_var.set(request_id)

        path: str = scope.get("path", "")
        method: str = scope.get("method", "")
        client = scope.get("client")
        client_ip = client[0] if client else None
        rid_bytes = request_id.encode("latin-1")

        status_holder = {"code": 500}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
                # Echo the id so a client/proxy can correlate too.
                raw = list(message.get("headers") or [])
                raw = [(k, v) for (k, v) in raw if k.lower() != _REQUEST_ID_HEADER]
                raw.append((_REQUEST_ID_HEADER, rid_bytes))
                message["headers"] = raw
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Let the framework's exception handlers still form the response;
            # we only record and re-raise so the error carries the request id.
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.exception(
                "http.error",
                extra={
                    "event": "http.error",
                    "method": method,
                    "path": path,
                    "duration_ms": duration_ms,
                    "client": client_ip,
                },
            )
            raise
        else:
            if path not in _SILENT_PATHS:
                duration_ms = round((time.perf_counter() - start) * 1000, 1)
                code = status_holder["code"]
                level = logging.WARNING if code >= 500 else logging.INFO
                logger.log(
                    level,
                    "http.access",
                    extra={
                        "event": "http.access",
                        "method": method,
                        "path": path,
                        "status": code,
                        "duration_ms": duration_ms,
                        "client": client_ip,
                    },
                )
        finally:
            request_id_var.reset(token)
