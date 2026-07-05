"""Fire-and-forget Celery enqueue for fastapp (no import of ``agriapi.tasks``).

A cut-over route that hands work to the background must enqueue the SAME task,
by name, with the SAME kwargs the Django side used via ``<task>.delay(...)`` —
so the existing Celery worker (which owns the task implementations) picks it up
unchanged. We only need a broker-configured ``Celery`` app to ``send_task``;
importing the task functions would drag Django into the sidecar.

Call this as ``celery.send_task(...)`` (attribute access at call time) so tests
can monkeypatch ``fastapp.celery.send_task`` to a no-op.
"""

from __future__ import annotations

from functools import lru_cache

from celery import Celery

from fastapp.settings import get_settings


@lru_cache(maxsize=1)
def _celery_app() -> Celery:
    """Broker-only Celery app (no task imports) — cached per process.

    Routes ``agriapi.*`` tasks to the ``agriapi`` queue, exactly like Django's
    ``CELERY_TASK_ROUTES`` — otherwise a bare ``send_task`` lands on the default
    ``celery`` queue, which the ``-Q agriapi`` worker never consumes, and the
    on-demand alert / zone-outbound tasks are silently dropped."""
    app = Celery(broker=get_settings().celery_broker_url)
    app.conf.task_routes = {"agriapi.*": {"queue": "agriapi"}}
    app.conf.task_default_queue = "agriapi"
    return app


def send_task(name: str, **kwargs) -> None:
    """Enqueue ``name`` with keyword args, mirroring ``<task>.delay(**kwargs)``
    on the Django side (same task name, same kwargs). Fire-and-forget."""
    _celery_app().send_task(name, kwargs=kwargs)
