"""Admin monitoring router (django-ninja) — mounted at ``/admin/monitoring``.

The observability surface for the back-office. Reads the three Wave-1 history
tables (analytics_taskrun / _notificationdeliverylog / _loginevent) plus the
Celery Beat schedule so the admin can see *what the system does on its own* and
*how users behave*. All routes require JWT + ``is_staff``.

Endpoints:
  * GET /admin/monitoring/overview     — ops health KPIs (tasks, deliveries, logins, fleet)
  * GET /admin/monitoring/tasks        — task-run history + per-task aggregates + beat schedule
  * GET /admin/monitoring/deliveries   — notification delivery log (filterable)
  * GET /admin/monitoring/logins       — sign-in events (filterable)
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q
from django.utils import timezone
from ninja import Router

from agriapi.api.auth import JwtAuth
from apps.irrigation.models import LoginEvent, NotificationDeliveryLog, TaskRun
from apps.irrigation.router_admin import _require_admin
from apps.lorawan.chirpstack.models import LoraUplink

router = Router()
log = logging.getLogger(__name__)
User = get_user_model()

OFFLINE_AFTER_HOURS = 24


def _rate(part: int, total: int) -> float:
    return round(100.0 * part / total, 1) if total else 0.0


# --- serializers -----------------------------------------------------------
def _task_run(t: TaskRun) -> dict[str, Any]:
    return {
        "id": t.id,
        "task_name": t.task_name,
        "status": t.status,
        "runtime_ms": t.runtime_ms,
        "result": t.result or {},
        "error": t.error or "",
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _delivery(d: NotificationDeliveryLog) -> dict[str, Any]:
    return {
        "id": d.id,
        "channel": d.channel,
        "kind": d.kind,
        "recipient": d.recipient,
        "username": getattr(d.user, "username", None),
        "status": d.status,
        "error": d.error or "",
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _login(e: LoginEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "username": e.username,
        "success": e.success,
        "ip": e.ip,
        "user_agent": e.user_agent,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def _fleet_summary(now) -> dict[str, Any]:
    """Online/offline counts keyed by distinct LoRaWAN dev_eui last-seen — same
    basis as /admin/devices/health (no dev_eui→user link exists on main)."""
    cutoff = now - timedelta(hours=OFFLINE_AFTER_HOURS)
    rows = LoraUplink.objects.values("dev_eui").annotate(last_seen=Max("received_at"))
    total = 0
    offline = 0
    for r in rows:
        total += 1
        if not r["last_seen"] or r["last_seen"] < cutoff:
            offline += 1
    return {
        "total": total,
        "offline": offline,
        "online": total - offline,
        "offline_pct": _rate(offline, total),
    }


# --- overview --------------------------------------------------------------
@router.get("/overview", auth=JwtAuth(), summary="Admin: monitoring overview")
def overview(request):
    guard = _require_admin(request)
    if guard:
        return guard

    now = timezone.now()
    since = now - timedelta(hours=24)

    # Tasks (24h)
    task_qs = TaskRun.objects.filter(created_at__gte=since)
    task_total = task_qs.count()
    task_fail = task_qs.filter(status=TaskRun.FAILURE).count()
    by_task = [
        {
            "task_name": row["task_name"],
            "runs": row["runs"],
            "failures": row["failures"],
            "success_rate": _rate(row["runs"] - row["failures"], row["runs"]),
        }
        for row in task_qs.values("task_name")
        .annotate(
            runs=Count("id"),
            failures=Count("id", filter=Q(status=TaskRun.FAILURE)),
        )
        .order_by("-runs")
    ]

    # Deliveries (24h)
    deliv_qs = NotificationDeliveryLog.objects.filter(created_at__gte=since)
    deliv_total = deliv_qs.count()
    deliv_fail = deliv_qs.filter(status=NotificationDeliveryLog.FAILED).count()
    by_channel = [
        {
            "channel": row["channel"],
            "total": row["total"],
            "failed": row["failed"],
            "error_rate": _rate(row["failed"], row["total"]),
        }
        for row in deliv_qs.values("channel")
        .annotate(
            total=Count("id"),
            failed=Count("id", filter=Q(status=NotificationDeliveryLog.FAILED)),
        )
        .order_by("-total")
    ]

    # Logins (24h)
    login_qs = LoginEvent.objects.filter(created_at__gte=since)
    login_total = login_qs.count()
    login_fail = login_qs.filter(success=False).count()
    unique_users = login_qs.filter(success=True).values("username").distinct().count()

    # Recent failures (tasks + deliveries), newest first
    recent_failures: list[dict[str, Any]] = []
    for t in TaskRun.objects.filter(status=TaskRun.FAILURE).order_by("-id")[:10]:
        recent_failures.append(
            {
                "type": "task",
                "name": t.task_name,
                "error": (t.error or "")[:300],
                "at": t.created_at.isoformat() if t.created_at else None,
            }
        )
    for d in NotificationDeliveryLog.objects.filter(
        status=NotificationDeliveryLog.FAILED
    ).order_by("-id")[:10]:
        recent_failures.append(
            {
                "type": "delivery",
                "name": f"{d.channel}/{d.kind}",
                "error": (d.error or "")[:300],
                "at": d.created_at.isoformat() if d.created_at else None,
            }
        )
    recent_failures.sort(key=lambda r: r["at"] or "", reverse=True)

    return {
        "window_hours": 24,
        "tasks": {
            "total": task_total,
            "failures": task_fail,
            "success_rate": _rate(task_total - task_fail, task_total),
            "by_task": by_task,
        },
        "deliveries": {
            "total": deliv_total,
            "failed": deliv_fail,
            "error_rate": _rate(deliv_fail, deliv_total),
            "by_channel": by_channel,
        },
        "logins": {
            "total": login_total,
            "failed": login_fail,
            "unique_users": unique_users,
        },
        "fleet": _fleet_summary(now),
        "recent_failures": recent_failures[:10],
    }


# --- task runs + schedule --------------------------------------------------
def _beat_schedule() -> list[dict[str, Any]]:
    """Serialize the configured Beat schedule. Falls back gracefully whether the
    static CELERY_BEAT_SCHEDULE (default scheduler / CI) or the
    DatabaseScheduler (prod) is in use."""
    entries: list[dict[str, Any]] = []
    static = getattr(settings, "CELERY_BEAT_SCHEDULE", {}) or {}
    for name, cfg in static.items():
        entries.append(
            {
                "name": name,
                "task": cfg.get("task"),
                "schedule": str(cfg.get("schedule")),
                "source": "static",
            }
        )
    # Prod runs django_celery_beat's DatabaseScheduler — surface those rows too.
    try:
        from django_celery_beat.models import PeriodicTask

        for pt in PeriodicTask.objects.filter(enabled=True):
            sched = pt.interval or pt.crontab or pt.solar or pt.clocked
            entries.append(
                {
                    "name": pt.name,
                    "task": pt.task,
                    "schedule": str(sched),
                    "source": "database",
                    "last_run_at": pt.last_run_at.isoformat()
                    if pt.last_run_at
                    else None,
                }
            )
    except Exception:  # pragma: no cover - app may be absent
        pass
    return entries


@router.get("/tasks", auth=JwtAuth(), summary="Admin: task-run history + schedule")
def tasks(
    request, task_name: str | None = None, status: str | None = None, limit: int = 100
):
    guard = _require_admin(request)
    if guard:
        return guard

    limit = max(1, min(limit, 500))
    qs = TaskRun.objects.all().order_by("-id")
    if task_name:
        qs = qs.filter(task_name=task_name)
    if status:
        qs = qs.filter(status=status)
    runs = [_task_run(t) for t in qs[:limit]]

    # 7-day per-task aggregates
    since = timezone.now() - timedelta(days=7)
    aggregates = [
        {
            "task_name": row["task_name"],
            "runs": row["runs"],
            "failures": row["failures"],
            "success_rate": _rate(row["runs"] - row["failures"], row["runs"]),
            "last_run": row["last_run"].isoformat() if row["last_run"] else None,
        }
        for row in TaskRun.objects.filter(created_at__gte=since)
        .values("task_name")
        .annotate(
            runs=Count("id"),
            failures=Count("id", filter=Q(status=TaskRun.FAILURE)),
            last_run=Max("created_at"),
        )
        .order_by("-runs")
    ]

    return {"runs": runs, "aggregates": aggregates, "schedule": _beat_schedule()}


# --- deliveries ------------------------------------------------------------
@router.get("/deliveries", auth=JwtAuth(), summary="Admin: notification delivery log")
def deliveries(
    request,
    channel: str | None = None,
    status: str | None = None,
    username: str | None = None,
    limit: int = 200,
):
    guard = _require_admin(request)
    if guard:
        return guard

    limit = max(1, min(limit, 1000))
    qs = NotificationDeliveryLog.objects.all().order_by("-id")
    if channel:
        qs = qs.filter(channel=channel)
    if status:
        qs = qs.filter(status=status)
    if username:
        qs = qs.filter(user__username=username)
    return [_delivery(d) for d in qs[:limit]]


# --- logins ----------------------------------------------------------------
@router.get("/logins", auth=JwtAuth(), summary="Admin: sign-in events")
def logins(
    request,
    username: str | None = None,
    success: bool | None = None,
    limit: int = 200,
):
    guard = _require_admin(request)
    if guard:
        return guard

    limit = max(1, min(limit, 1000))
    qs = LoginEvent.objects.all().order_by("-id")
    if username:
        qs = qs.filter(username=username)
    if success is not None:
        qs = qs.filter(success=success)
    return [_login(e) for e in qs[:limit]]
