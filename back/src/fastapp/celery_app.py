"""Native (Django-free) Celery app for the fastapp worker (F10).

Registers the ported task bodies (fastapp/tasks_comms + tasks_compute +
tasks_scan) under the SAME task names the rest of the system already uses
(``agriapi.tasks.<name>``), so the wire contract with the enqueuers is
unchanged: fastapp routers ``send_task("agriapi.tasks.send_alert_email", ...)``
and beat's periodic entries keep resolving after the worker is switched from the
Django app to this one.

Broker + result backend + queue routing mirror ``agriapi.celery`` /
``settings.CELERY_*`` so both apps interoperate during the overlap (same broker,
same ``agriapi`` queue). Task-run history is recorded to ``analytics_taskrun``
from Celery signals, fail-soft, so the monitoring console keeps working.
"""

from __future__ import annotations

import datetime
import logging
import time

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    setup_logging,
    task_failure,
    task_postrun,
    task_prerun,
)

from fastapp import tasks_comms, tasks_compute, tasks_scan
from fastapp.logging_config import configure_logging
from fastapp.settings import get_settings

log = logging.getLogger(__name__)
_settings = get_settings()


@setup_logging.connect
def _configure_worker_logging(**_kwargs):
    """Own the worker/beat logging setup. Connecting to ``setup_logging`` tells
    Celery NOT to install its own handlers, so our JSON formatter (with the
    request-id filter) is the single formatter — task log lines land in Loki
    with the same shape as the web sidecar's."""
    configure_logging(_settings.log_level, _settings.log_format, force=True)


# set_as_current=False: creating this app must NOT hijack the global current_app.
# The Django app (agriapi) and this one register the SAME task names
# (agriapi.tasks.*) — in a single process (the test suite imports both) whichever
# app is "current" wins name resolution. The fastapp worker activates this app
# explicitly via `celery -A fastapp.celery_app`, so prod is unaffected; leaving it
# non-current keeps the Django task tests resolving to the Django tasks.
app = Celery("fastapp", broker=_settings.celery_broker_url, set_as_current=False)
app.conf.result_backend = _settings.celery_broker_url
app.conf.timezone = "UTC"
# Same queue + routing as the Django app so enqueuers/beat and this worker meet.
app.conf.task_routes = {"agriapi.*": {"queue": "agriapi"}}
app.conf.task_default_queue = "agriapi"

# --- register the ported bodies under their wire-contract names --------------
_TASK_BODIES = {
    # on-demand comms (enqueued by ingest.py + notifications router)
    "agriapi.tasks.send_alert_email": tasks_comms.send_alert_email,
    "agriapi.tasks.send_alert_digest_email": tasks_comms.send_alert_digest_email,
    "agriapi.tasks.send_alert_whatsapp": tasks_comms.send_alert_whatsapp,
    "agriapi.tasks.send_alert_sms": tasks_comms.send_alert_sms,
    "agriapi.tasks.send_zone_outbound_email": tasks_comms.send_zone_outbound_email,
    "agriapi.tasks.send_zone_outbound_sms": tasks_comms.send_zone_outbound_sms,
    "agriapi.tasks.send_zone_outbound_whatsapp": tasks_comms.send_zone_outbound_whatsapp,
    # periodic compute / scan (beat)
    "agriapi.tasks.compute_et0_vpd_hourly": tasks_compute.compute_et0_vpd_hourly,
    "agriapi.tasks.send_periodic_notifications": tasks_compute.send_periodic_notifications,
    "agriapi.tasks.flag_idle_zones": tasks_compute.flag_idle_zones,
    "agriapi.tasks.scan_device_health": tasks_scan.scan_device_health,
    "agriapi.tasks.scan_proactive_insights": tasks_scan.scan_proactive_insights,
    "agriapi.tasks.run_due_irrigation_programs": tasks_scan.run_due_irrigation_programs,
}

for _name, _fn in _TASK_BODIES.items():
    app.task(name=_name)(_fn)

# --- static beat schedule ----------------------------------------------------
# Cadences MATCH THE LIVE prod PeriodicTask rows (django_celery_beat
# DatabaseScheduler), captured from the droplet on 2026-07-05 — NOT the prod
# crontab branch in settings/base.py, which the DatabaseScheduler ignored. This
# preserves the exact current behaviour when the beat container is switched from
# the DatabaseScheduler to this static PersistentScheduler. The dev-only
# simulate_sensor_ingest is intentionally omitted; celery.backend_cleanup is
# provided by Celery itself.
app.conf.beat_schedule = {
    "compute_et0_vpd_hourly": {
        "task": "agriapi.tasks.compute_et0_vpd_hourly",
        "schedule": crontab(minute="*/4"),
    },
    "send_periodic_notifications": {
        "task": "agriapi.tasks.send_periodic_notifications",
        "schedule": crontab(minute="*/4"),
    },
    "scan_device_health": {
        "task": "agriapi.tasks.scan_device_health",
        "schedule": crontab(minute="*/10"),
    },
    "scan_proactive_insights": {
        "task": "agriapi.tasks.scan_proactive_insights",
        "schedule": crontab(minute="*/10"),
    },
    "run_due_irrigation_programs": {
        "task": "agriapi.tasks.run_due_irrigation_programs",
        "schedule": crontab(minute="*/2"),
    },
    "flag_idle_zones": {
        "task": "agriapi.tasks.flag_idle_zones",
        "schedule": crontab(minute="*/10"),
    },
}


# --- task-run history (analytics_taskrun) ------------------------------------
_started_at: dict[str, float] = {}


def _truncate(value, limit: int = 4000) -> str:
    return str(value)[:limit] if value is not None else ""


@task_prerun.connect
def _record_task_start(task_id=None, **_kwargs):
    if task_id:
        _started_at[task_id] = time.monotonic()


@task_postrun.connect
def _record_task_run(task_id=None, task=None, state=None, retval=None, **_kwargs):
    started = _started_at.pop(task_id, None)
    if state == "FAILURE":
        return  # recorded by _record_task_failure
    try:
        from agri.core.database import session_scope
        from agri.db.audit import AnalyticsTaskrun

        runtime_ms = int((time.monotonic() - started) * 1000) if started else None
        result = retval if isinstance(retval, dict) else {"value": _truncate(retval)}
        import json

        now = datetime.datetime.now(datetime.timezone.utc)
        with session_scope(commit=True) as session:
            session.add(
                AnalyticsTaskrun(
                    task_name=(getattr(task, "name", "") or "")[:128],
                    status="success",
                    finished_at=now,
                    runtime_ms=runtime_ms,
                    result=json.dumps(result)[:4000],
                    error="",
                    created_at=now,
                )
            )
    except Exception:  # pragma: no cover - fail-soft
        log.warning("task-run record failed", exc_info=True)


@task_failure.connect
def _record_task_failure(task_id=None, exception=None, sender=None, **_kwargs):
    started = _started_at.pop(task_id, None)
    try:
        from agri.core.database import session_scope
        from agri.db.audit import AnalyticsTaskrun

        runtime_ms = int((time.monotonic() - started) * 1000) if started else None
        now = datetime.datetime.now(datetime.timezone.utc)
        with session_scope(commit=True) as session:
            session.add(
                AnalyticsTaskrun(
                    task_name=(getattr(sender, "name", "") or "")[:128],
                    status="failure",
                    finished_at=now,
                    runtime_ms=runtime_ms,
                    result="",
                    error=_truncate(exception),
                    created_at=now,
                )
            )
    except Exception:  # pragma: no cover - fail-soft
        log.warning("task-run failure record failed", exc_info=True)
