"""fastapp /admin/monitoring — observability surface (staff only).

Strangler port of ``apps/irrigation/router_monitoring.py`` (django-ninja).
Byte-parity with the Django version: same routes, same KPI aggregates, same
task-run / delivery / login serialize shapes, and the SAME Celery-Beat schedule
view (static ``CELERY_BEAT_SCHEDULE`` + ``django_celery_beat`` DatabaseScheduler
rows). Reads go through the agri-core SQLAlchemy session (no Django ORM); the
static schedule is rebuilt from the SAME env-driven crontab logic Django's
settings use, and the DatabaseScheduler rows are read via raw SQL with each
schedule's ``__str__`` reconstructed exactly.
"""

from __future__ import annotations

import datetime
import os
from typing import Any

from celery.schedules import crontab
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text

from agri.core.database import session_scope
from agri.db.audit import (
    AnalyticsLoginevent,
    AnalyticsNotificationdeliverylog,
    AnalyticsTaskrun,
)
from fastapp.adminutil import username_for
from fastapp.auth import AuthedUser, get_current_staff_user

router = APIRouter(tags=["admin-monitoring"])

OFFLINE_AFTER_HOURS = 24
_FAILURE = "failure"  # TaskRun.FAILURE
_FAILED = "failed"  # NotificationDeliveryLog.FAILED


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _rate(part: int, total: int) -> float:
    return round(100.0 * part / total, 1) if total else 0.0


# --- serializers -----------------------------------------------------------
def _task_run(t: AnalyticsTaskrun) -> dict[str, Any]:
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


def _delivery(session, d: AnalyticsNotificationdeliverylog) -> dict[str, Any]:
    return {
        "id": d.id,
        "channel": d.channel,
        "kind": d.kind,
        "recipient": d.recipient,
        "username": username_for(session, d.user_id),
        "status": d.status,
        "error": d.error or "",
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _login(e: AnalyticsLoginevent) -> dict[str, Any]:
    return {
        "id": e.id,
        "username": e.username,
        "success": e.success,
        "ip": e.ip,
        "user_agent": e.user_agent,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def _fleet_summary(session, now: datetime.datetime) -> dict[str, Any]:
    """Online/offline counts keyed by distinct LoRaWAN dev_eui last-seen (same
    basis as Django's ``_fleet_summary`` over the unmanaged ``lora_uplink``)."""
    cutoff = now - datetime.timedelta(hours=OFFLINE_AFTER_HOURS)
    rows = session.execute(
        text(
            "SELECT dev_eui, MAX(received_at) AS last_seen "
            "FROM lora_uplink GROUP BY dev_eui"
        )
    ).all()
    total = 0
    offline = 0
    for r in rows:
        total += 1
        if not r.last_seen or r.last_seen < cutoff:
            offline += 1
    return {
        "total": total,
        "offline": offline,
        "online": total - offline,
        "offline_pct": _rate(offline, total),
    }


# --- overview --------------------------------------------------------------
@router.get("/admin/monitoring/overview", summary="Admin: monitoring overview")
def overview(user: AuthedUser = Depends(get_current_staff_user)):
    now = _utcnow()
    since = now - datetime.timedelta(hours=24)
    with session_scope() as session:
        # Tasks (24h)
        task_total = session.scalar(
            select(func.count())
            .select_from(AnalyticsTaskrun)
            .where(AnalyticsTaskrun.created_at >= since)
        )
        task_fail = session.scalar(
            select(func.count())
            .select_from(AnalyticsTaskrun)
            .where(
                AnalyticsTaskrun.created_at >= since,
                AnalyticsTaskrun.status == _FAILURE,
            )
        )
        by_task = [
            {
                "task_name": row.task_name,
                "runs": row.runs,
                "failures": row.failures,
                "success_rate": _rate(row.runs - row.failures, row.runs),
            }
            for row in session.execute(
                select(
                    AnalyticsTaskrun.task_name,
                    func.count(AnalyticsTaskrun.id).label("runs"),
                    func.count(AnalyticsTaskrun.id)
                    .filter(AnalyticsTaskrun.status == _FAILURE)
                    .label("failures"),
                )
                .where(AnalyticsTaskrun.created_at >= since)
                .group_by(AnalyticsTaskrun.task_name)
                .order_by(func.count(AnalyticsTaskrun.id).desc())
            ).all()
        ]

        # Deliveries (24h)
        deliv_total = session.scalar(
            select(func.count())
            .select_from(AnalyticsNotificationdeliverylog)
            .where(AnalyticsNotificationdeliverylog.created_at >= since)
        )
        deliv_fail = session.scalar(
            select(func.count())
            .select_from(AnalyticsNotificationdeliverylog)
            .where(
                AnalyticsNotificationdeliverylog.created_at >= since,
                AnalyticsNotificationdeliverylog.status == _FAILED,
            )
        )
        by_channel = [
            {
                "channel": row.channel,
                "total": row.total,
                "failed": row.failed,
                "error_rate": _rate(row.failed, row.total),
            }
            for row in session.execute(
                select(
                    AnalyticsNotificationdeliverylog.channel,
                    func.count(AnalyticsNotificationdeliverylog.id).label("total"),
                    func.count(AnalyticsNotificationdeliverylog.id)
                    .filter(AnalyticsNotificationdeliverylog.status == _FAILED)
                    .label("failed"),
                )
                .where(AnalyticsNotificationdeliverylog.created_at >= since)
                .group_by(AnalyticsNotificationdeliverylog.channel)
                .order_by(func.count(AnalyticsNotificationdeliverylog.id).desc())
            ).all()
        ]

        # Logins (24h)
        login_total = session.scalar(
            select(func.count())
            .select_from(AnalyticsLoginevent)
            .where(AnalyticsLoginevent.created_at >= since)
        )
        login_fail = session.scalar(
            select(func.count())
            .select_from(AnalyticsLoginevent)
            .where(
                AnalyticsLoginevent.created_at >= since,
                AnalyticsLoginevent.success.is_(False),
            )
        )
        unique_users = session.scalar(
            select(func.count(func.distinct(AnalyticsLoginevent.username))).where(
                AnalyticsLoginevent.created_at >= since,
                AnalyticsLoginevent.success.is_(True),
            )
        )

        # Recent failures (tasks + deliveries), newest first
        recent_failures: list[dict[str, Any]] = []
        for t in session.scalars(
            select(AnalyticsTaskrun)
            .where(AnalyticsTaskrun.status == _FAILURE)
            .order_by(AnalyticsTaskrun.id.desc())
            .limit(10)
        ).all():
            recent_failures.append(
                {
                    "type": "task",
                    "name": t.task_name,
                    "error": (t.error or "")[:300],
                    "at": t.created_at.isoformat() if t.created_at else None,
                }
            )
        for d in session.scalars(
            select(AnalyticsNotificationdeliverylog)
            .where(AnalyticsNotificationdeliverylog.status == _FAILED)
            .order_by(AnalyticsNotificationdeliverylog.id.desc())
            .limit(10)
        ).all():
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
            "fleet": _fleet_summary(session, now),
            "recent_failures": recent_failures[:10],
        }


# --- beat schedule (static settings + DatabaseScheduler rows) ---------------
def _static_beat_schedule() -> list[dict[str, Any]]:
    """Rebuild the static ``CELERY_BEAT_SCHEDULE`` exactly as
    ``agriapi/settings/base.py`` does — same env vars, same crontab objects, so
    ``str(schedule)`` is byte-identical to Django's."""
    mode = os.getenv("CELERY_SCHEDULE_MODE", "test").lower()
    if mode == "test":
        et0 = crontab(minute="*/4")
        email = crontab(minute="*/4")
        sim = crontab(minute="*/2")
        devh = crontab(minute="*/10")
        pro = crontab(minute="*/10")
        irr = crontab(minute="*/2")
        idle = crontab(minute="*/10")
    else:
        et0 = crontab(minute=0)
        email = crontab(minute="*/5")
        sim = crontab(minute="*/15")
        devh = crontab(minute=15)
        pro = crontab(minute=30, hour="*/8")
        irr = crontab(minute="*/5")
        idle = crontab(minute=45, hour="*/6")
    schedule: dict[str, dict[str, Any]] = {
        "compute_et0": {
            "task": "agriapi.tasks.compute_et0_vpd_hourly",
            "schedule": et0,
        },
        "email_ping": {
            "task": "agriapi.tasks.send_periodic_notifications",
            "schedule": email,
        },
        "device_health_scan": {
            "task": "agriapi.tasks.scan_device_health",
            "schedule": devh,
        },
        "proactive_scan": {
            "task": "agriapi.tasks.scan_proactive_insights",
            "schedule": pro,
        },
        "irrigation_run": {
            "task": "agriapi.tasks.run_due_irrigation_programs",
            "schedule": irr,
        },
        "idle_zone_scan": {
            "task": "agriapi.tasks.flag_idle_zones",
            "schedule": idle,
        },
    }
    if os.getenv("ENABLE_SENSOR_SIMULATOR", "False").lower() in ("1", "true", "yes"):
        schedule["simulate_sensors"] = {
            "task": "agriapi.tasks.simulate_sensor_ingest",
            "schedule": sim,
        }
    return [
        {
            "name": name,
            "task": cfg.get("task"),
            "schedule": str(cfg.get("schedule")),
            "source": "static",
        }
        for name, cfg in schedule.items()
    ]


def _cronexp(field: Any) -> str:
    """django_celery_beat.models.cronexp — ``field or '*'`` with spaces removed."""
    return (field and str(field).replace(" ", "")) or "*"


def _interval_str(every: int, period: str) -> str:
    """django_celery_beat IntervalSchedule.__str__."""
    if every == 1:
        return f"every {period[:-1]}"
    return f"every {every} {period}"


def _crontab_str(row) -> str:
    """django_celery_beat CrontabSchedule.__str__ (order: m h dM MY d + tz)."""
    return "{} {} {} {} {} (m/h/dM/MY/d) {}".format(
        _cronexp(row.minute),
        _cronexp(row.hour),
        _cronexp(row.day_of_month),
        _cronexp(row.month_of_year),
        _cronexp(row.day_of_week),
        str(row.timezone),
    )


def _database_beat_schedule(session) -> list[dict[str, Any]]:
    """Read enabled ``django_celery_beat`` PeriodicTask rows via raw SQL and
    reconstruct each schedule's ``__str__`` (interval → crontab → solar →
    clocked, mirroring ``pt.interval or pt.crontab or pt.solar or pt.clocked``)."""
    # The table may be absent in some envs — degrade gracefully (Django wraps
    # the whole block in try/except too).
    try:
        rows = session.execute(
            text(
                "SELECT pt.name, pt.task, pt.last_run_at, "
                "i.every AS i_every, i.period AS i_period, "
                "c.minute, c.hour, c.day_of_week, c.day_of_month, "
                "c.month_of_year, c.timezone, "
                "s.event AS s_event, s.latitude AS s_lat, s.longitude AS s_lon, "
                "cl.clocked_time "
                "FROM django_celery_beat_periodictask pt "
                "LEFT JOIN django_celery_beat_intervalschedule i "
                "  ON i.id = pt.interval_id "
                "LEFT JOIN django_celery_beat_crontabschedule c "
                "  ON c.id = pt.crontab_id "
                "LEFT JOIN django_celery_beat_solarschedule s "
                "  ON s.id = pt.solar_id "
                "LEFT JOIN django_celery_beat_clockedschedule cl "
                "  ON cl.id = pt.clocked_id "
                "WHERE pt.enabled = true "
                "ORDER BY pt.id"
            )
        ).all()
    except Exception:  # pragma: no cover - app/table may be absent
        return []

    entries: list[dict[str, Any]] = []
    for r in rows:
        if r.i_every is not None:
            sched = _interval_str(r.i_every, r.i_period)
        elif r.minute is not None:
            sched = _crontab_str(r)
        elif r.s_event is not None:
            sched = f"{r.s_event} ({r.s_lat}, {r.s_lon})"
        elif r.clocked_time is not None:
            sched = str(r.clocked_time)
        else:
            sched = "None"
        entries.append(
            {
                "name": r.name,
                "task": r.task,
                "schedule": sched,
                "source": "database",
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
            }
        )
    return entries


def _beat_schedule(session) -> list[dict[str, Any]]:
    return _static_beat_schedule() + _database_beat_schedule(session)


# --- task runs + schedule --------------------------------------------------
@router.get("/admin/monitoring/tasks", summary="Admin: task-run history + schedule")
def tasks(
    task_name: str | None = None,
    status: str | None = None,
    limit: int = 100,
    user: AuthedUser = Depends(get_current_staff_user),
):
    limit = max(1, min(limit, 500))
    with session_scope() as session:
        criteria = []
        if task_name:
            criteria.append(AnalyticsTaskrun.task_name == task_name)
        if status:
            criteria.append(AnalyticsTaskrun.status == status)
        runs = [
            _task_run(t)
            for t in session.scalars(
                select(AnalyticsTaskrun)
                .where(*criteria)
                .order_by(AnalyticsTaskrun.id.desc())
                .limit(limit)
            ).all()
        ]

        since = _utcnow() - datetime.timedelta(days=7)
        aggregates = [
            {
                "task_name": row.task_name,
                "runs": row.runs,
                "failures": row.failures,
                "success_rate": _rate(row.runs - row.failures, row.runs),
                "last_run": row.last_run.isoformat() if row.last_run else None,
            }
            for row in session.execute(
                select(
                    AnalyticsTaskrun.task_name,
                    func.count(AnalyticsTaskrun.id).label("runs"),
                    func.count(AnalyticsTaskrun.id)
                    .filter(AnalyticsTaskrun.status == _FAILURE)
                    .label("failures"),
                    func.max(AnalyticsTaskrun.created_at).label("last_run"),
                )
                .where(AnalyticsTaskrun.created_at >= since)
                .group_by(AnalyticsTaskrun.task_name)
                .order_by(func.count(AnalyticsTaskrun.id).desc())
            ).all()
        ]

        return {
            "runs": runs,
            "aggregates": aggregates,
            "schedule": _beat_schedule(session),
        }


# --- deliveries ------------------------------------------------------------
@router.get("/admin/monitoring/deliveries", summary="Admin: notification delivery log")
def deliveries(
    channel: str | None = None,
    status: str | None = None,
    username: str | None = None,
    limit: int = 200,
    user: AuthedUser = Depends(get_current_staff_user),
):
    limit = max(1, min(limit, 1000))
    with session_scope() as session:
        criteria = []
        if channel:
            criteria.append(AnalyticsNotificationdeliverylog.channel == channel)
        if status:
            criteria.append(AnalyticsNotificationdeliverylog.status == status)
        if username:
            from agri.db.users import CustomUserCustomuser

            resolved = session.scalar(
                select(CustomUserCustomuser.id).where(
                    CustomUserCustomuser.username == username
                )
            )
            criteria.append(
                AnalyticsNotificationdeliverylog.user_id
                == (resolved if resolved is not None else -1)
            )
        rows = session.scalars(
            select(AnalyticsNotificationdeliverylog)
            .where(*criteria)
            .order_by(AnalyticsNotificationdeliverylog.id.desc())
            .limit(limit)
        ).all()
        return [_delivery(session, d) for d in rows]


# --- logins ----------------------------------------------------------------
@router.get("/admin/monitoring/logins", summary="Admin: sign-in events")
def logins(
    username: str | None = None,
    success: bool | None = None,
    limit: int = 200,
    user: AuthedUser = Depends(get_current_staff_user),
):
    limit = max(1, min(limit, 1000))
    with session_scope() as session:
        criteria = []
        if username:
            criteria.append(AnalyticsLoginevent.username == username)
        if success is not None:
            criteria.append(AnalyticsLoginevent.success.is_(success))
        rows = session.scalars(
            select(AnalyticsLoginevent)
            .where(*criteria)
            .order_by(AnalyticsLoginevent.id.desc())
            .limit(limit)
        ).all()
        return [_login(e) for e in rows]
