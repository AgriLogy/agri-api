import logging
import os
import time

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agriapi.settings")

app = Celery("agriapi")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["agriapi"])

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Task-run history (Wave 1 monitoring back-office)
# ---------------------------------------------------------------------------
# Persist one analytics_taskrun row per task execution so the admin console can
# show "what runs, how often, and how it's been doing". Captured centrally here
# from Celery signals — no per-task edits. Fail-soft: never break a task because
# the (unmanaged, self-deployed) table is missing or the write fails.

_started_at: dict[str, float] = {}


def _truncate(value, limit: int = 4000):
    text = str(value)
    return text[:limit] if value is not None else ""


@task_prerun.connect
def _record_task_start(task_id=None, **_kwargs):
    if task_id:
        _started_at[task_id] = time.monotonic()


@task_postrun.connect
def _record_task_run(task_id=None, task=None, state=None, retval=None, **_kwargs):
    started = _started_at.pop(task_id, None)
    # task_failure handles failures with the real exception; only log successes
    # here so a single run isn't double-recorded.
    if state == "FAILURE":
        return
    try:
        from django.utils import timezone

        from apps.irrigation.models import TaskRun

        runtime_ms = int((time.monotonic() - started) * 1000) if started else None
        result = retval if isinstance(retval, dict) else {"value": _truncate(retval)}
        TaskRun.objects.create(
            task_name=(getattr(task, "name", "") or "")[:128],
            status=TaskRun.SUCCESS,
            finished_at=timezone.now(),
            runtime_ms=runtime_ms,
            result=result,
        )
    except Exception as exc:  # pragma: no cover - fail-soft
        log.warning("taskrun record failed for %s: %s", task_id, exc)


@task_failure.connect
def _record_task_failure(task_id=None, exception=None, sender=None, **_kwargs):
    started = _started_at.pop(task_id, None)
    try:
        from django.utils import timezone

        from apps.irrigation.models import TaskRun

        runtime_ms = int((time.monotonic() - started) * 1000) if started else None
        TaskRun.objects.create(
            task_name=(getattr(sender, "name", "") or "")[:128],
            status=TaskRun.FAILURE,
            finished_at=timezone.now(),
            runtime_ms=runtime_ms,
            error=_truncate(exception),
        )
    except Exception as exc:  # pragma: no cover - fail-soft
        log.warning("taskrun failure record failed for %s: %s", task_id, exc)
