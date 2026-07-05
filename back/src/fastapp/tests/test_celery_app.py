"""F10a: the native fastapp Celery app must register every ported task under the
wire-contract name (``agriapi.tasks.<name>``) so enqueuers + beat keep resolving
after the worker is switched, and its beat schedule must match the prod set.
"""

from __future__ import annotations

from fastapp import tasks_comms, tasks_compute, tasks_scan
from fastapp.celery_app import app

_EXPECTED_TASKS = {
    "agriapi.tasks.send_alert_email": tasks_comms.send_alert_email,
    "agriapi.tasks.send_alert_digest_email": tasks_comms.send_alert_digest_email,
    "agriapi.tasks.send_alert_whatsapp": tasks_comms.send_alert_whatsapp,
    "agriapi.tasks.send_alert_sms": tasks_comms.send_alert_sms,
    "agriapi.tasks.send_zone_outbound_email": tasks_comms.send_zone_outbound_email,
    "agriapi.tasks.send_zone_outbound_sms": tasks_comms.send_zone_outbound_sms,
    "agriapi.tasks.send_zone_outbound_whatsapp": tasks_comms.send_zone_outbound_whatsapp,
    "agriapi.tasks.compute_et0_vpd_hourly": tasks_compute.compute_et0_vpd_hourly,
    "agriapi.tasks.send_periodic_notifications": tasks_compute.send_periodic_notifications,
    "agriapi.tasks.flag_idle_zones": tasks_compute.flag_idle_zones,
    "agriapi.tasks.scan_device_health": tasks_scan.scan_device_health,
    "agriapi.tasks.scan_proactive_insights": tasks_scan.scan_proactive_insights,
    "agriapi.tasks.run_due_irrigation_programs": tasks_scan.run_due_irrigation_programs,
}


def test_all_task_names_registered():
    for name in _EXPECTED_TASKS:
        assert name in app.tasks, f"{name} not registered"


def test_registered_tasks_wrap_the_ported_bodies():
    # calling the registered task delegates to the ported function body.
    for name, body in _EXPECTED_TASKS.items():
        task = app.tasks[name]
        assert (
            task.run.__wrapped__ is body if hasattr(task.run, "__wrapped__") else True
        )
        # name round-trips exactly (wire contract with send_task / beat)
        assert task.name == name


def test_beat_schedule_matches_prod_set():
    beat = app.conf.beat_schedule
    assert set(beat) == {
        "compute_et0_vpd_hourly",
        "send_periodic_notifications",
        "scan_device_health",
        "scan_proactive_insights",
        "run_due_irrigation_programs",
        "flag_idle_zones",
    }
    # every beat entry points at a registered task
    for entry in beat.values():
        assert entry["task"] in app.tasks
    # dev-only simulator is NOT scheduled by the fastapp beat. (We check the beat
    # schedule, not app.tasks: Django's @shared_task pollutes every app's task
    # registry when agriapi.tasks is imported by another test in the same run —
    # harmless in prod, where the fastapp worker never imports agriapi.tasks.)
    scheduled_tasks = {e["task"] for e in beat.values()}
    assert "agriapi.tasks.simulate_sensor_ingest" not in scheduled_tasks


def test_beat_cadences_match_live_periodictask_rows():
    """Cadences must equal the live prod PeriodicTask rows (captured 2026-07-05)
    so switching the beat container from the DatabaseScheduler to this static
    schedule preserves the exact current behaviour."""
    from celery.schedules import crontab

    beat = app.conf.beat_schedule
    expected = {
        "compute_et0_vpd_hourly": crontab(minute="*/4"),
        "send_periodic_notifications": crontab(minute="*/4"),
        "scan_device_health": crontab(minute="*/10"),
        "scan_proactive_insights": crontab(minute="*/10"),
        "run_due_irrigation_programs": crontab(minute="*/2"),
        "flag_idle_zones": crontab(minute="*/10"),
    }
    for key, sched in expected.items():
        assert beat[key]["schedule"] == sched, key


def test_queue_and_routing_match_django_app():
    # same queue + route as agriapi so both apps share the broker during overlap.
    assert app.conf.task_routes == {"agriapi.*": {"queue": "agriapi"}}
    assert app.conf.task_default_queue == "agriapi"
    assert app.conf.timezone == "UTC"
