"""The fastapp on-demand enqueue helper must route agriapi.* tasks to the
`agriapi` queue (like Django's CELERY_TASK_ROUTES), so the -Q agriapi worker
actually consumes the alert / zone-outbound tasks it enqueues.
"""

from __future__ import annotations

from fastapp import celery


def test_enqueue_app_routes_agriapi_tasks_to_agriapi_queue():
    app = celery._celery_app()
    assert app.conf.task_routes == {"agriapi.*": {"queue": "agriapi"}}
    assert app.conf.task_default_queue == "agriapi"


def test_send_task_enqueues_on_the_agriapi_queue(monkeypatch):
    captured = {}

    def _fake_send_task(self, name, kwargs=None, **opts):
        # celery resolves the queue from task_routes at send time; emulate it.
        route = self.conf.task_routes.get("agriapi.*", {})
        captured["name"] = name
        captured["queue"] = route.get("queue") or self.conf.task_default_queue
        captured["kwargs"] = kwargs

    from celery import Celery

    monkeypatch.setattr(Celery, "send_task", _fake_send_task, raising=True)
    celery._celery_app.cache_clear()
    celery.send_task("agriapi.tasks.send_alert_email", alert_id=1, value=2.0)
    assert captured["name"] == "agriapi.tasks.send_alert_email"
    assert captured["queue"] == "agriapi"
    assert captured["kwargs"] == {"alert_id": 1, "value": 2.0}
    celery._celery_app.cache_clear()
