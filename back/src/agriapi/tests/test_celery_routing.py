"""Celery queue-isolation routing.

This worker shares a redis broker with the legacy agriback monolith on the
default `celery` queue. We route every agriapi.* task onto a dedicated
`agriapi` queue (and start the worker with `-Q agriapi`) so the two stacks
stop stealing and rejecting each other's tasks. These tests lock that routing
so it can't silently regress.
"""

from __future__ import annotations

from agriapi.celery import app


def _queue_for(task_name: str) -> str:
    route = app.amqp.router.route({}, task_name)
    queue = route.get("queue")
    # router may hand back a Queue object or a bare name depending on config
    return getattr(queue, "name", queue)


def test_agriapi_tasks_route_to_dedicated_queue():
    assert _queue_for("agriapi.tasks.send_periodic_notifications") == "agriapi"
    assert _queue_for("agriapi.tasks.compute_et0_vpd_hourly") == "agriapi"


def test_legacy_tasks_stay_on_default_queue():
    # agriBack.* belongs to the legacy worker — it must NOT be pulled onto our
    # queue, or the demo simulator would stop running.
    assert _queue_for("agriBack.tasks.simulate_sensor_ingest") != "agriapi"
