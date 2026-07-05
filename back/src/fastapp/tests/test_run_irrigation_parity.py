"""F8b parity: ``run_due_irrigation_programs`` — the fastapp port must fire the
same due programs as the Django task (enabled, weekday-allowed, start_time in the
just-passed window, not already fired this window), create the same scheduled
OutputCommand, and dispatch it (simulated by default). fired/skipped identical.

The dedup claim mutates last_run_at and both create OutputCommand rows (both
unmanaged), so those are reset/wiped between the two runs.
"""

from __future__ import annotations

import datetime

import pytest
from django.conf import settings as dj_settings

from fastapp import tasks_scan as fp

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _clean_unmanaged(db):
    def _wipe():
        from apps.irrigation.models import IrrigationProgram, OutputCommand

        OutputCommand.objects.all().delete()
        IrrigationProgram.objects.all().delete()

    _wipe()
    yield
    _wipe()


def _mk_zone(django_user_model, uname):
    from apps.irrigation.models import Zone

    u = django_user_model.objects.create_user(
        username=uname, email=f"{uname}@x.com", password="x"
    )
    z = Zone.objects.create(
        user=u,
        name=f"Z-{uname}",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
        elevation_m=120.0,
    )
    return u, z


def _mk_program(u, z, name, start_time, **over):
    from apps.irrigation.models import IrrigationProgram

    payload = dict(
        user=u,
        zone=z,
        name=name,
        start_time=start_time,
        enabled=True,
        weekdays="",  # every day
    )
    payload.update(over)
    return IrrigationProgram.objects.create(**payload)


def _cmd_rows():
    from apps.irrigation.models import OutputCommand

    return sorted((c.action, c.source, c.status) for c in OutputCommand.objects.all())


def _reset(programs):
    from apps.irrigation.models import IrrigationProgram, OutputCommand

    OutputCommand.objects.all().delete()
    for p in programs:
        IrrigationProgram.objects.filter(pk=p.pk).update(last_run_at=p.last_run_at)


def test_run_due_irrigation_programs_identical(django_user_model):
    import agriapi.tasks as dj
    from apps.irrigation.models import IrrigationProgram, OutputCommand

    now = datetime.datetime.now(datetime.timezone.utc)
    in_window = now.time()  # start_dt ≈ now → inside [now-15min, now]
    past = (now - datetime.timedelta(hours=2)).time()

    u, z = _mk_zone(django_user_model, "irr")
    # P1 due (in window, never run) → fired
    p1 = _mk_program(u, z, "P1", in_window)
    # P2 in window but already fired this window → skipped
    p2 = _mk_program(u, z, "P2", in_window)
    IrrigationProgram.objects.filter(pk=p2.pk).update(last_run_at=now)
    # P3 disabled → not processed
    _mk_program(u, z, "P3", in_window, enabled=False)
    # P4 enabled but start_time 2h ago → outside window (no count)
    _mk_program(u, z, "P4", past)

    programs_snapshot = list(IrrigationProgram.objects.all())

    dj_res = dj.run_due_irrigation_programs()
    dj_cmds = _cmd_rows()

    _reset(programs_snapshot)  # restore last_run_at + wipe commands
    fp_res = fp.run_due_irrigation_programs()
    fp_cmds = _cmd_rows()

    assert dj_res == fp_res == {"fired": 1, "skipped": 1}
    # one scheduled command, simulated by default
    assert dj_cmds == fp_cmds == [("open", "scheduled", "simulated")]
    assert IrrigationProgram.objects.get(pk=p1.pk).last_run_at is not None


def test_run_due_irrigation_none_due(django_user_model):
    import agriapi.tasks as dj

    now = datetime.datetime.now(datetime.timezone.utc)
    past = (now - datetime.timedelta(hours=3)).time()
    u, z = _mk_zone(django_user_model, "irr")
    _mk_program(u, z, "P", past)  # outside window
    assert (
        dj.run_due_irrigation_programs()
        == fp.run_due_irrigation_programs()
        == {
            "fired": 0,
            "skipped": 0,
        }
    )
