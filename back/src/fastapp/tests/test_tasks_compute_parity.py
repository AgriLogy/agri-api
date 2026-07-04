"""F8b parity: the fastapp compute/scan task bodies vs their Django
``agriapi.tasks`` twins. Both delegate the physics to the SAME agri-core
handler, so this asserts the persistence (upsert / idempotency) is identical:
same return dict, same Et0Calculated / VPDWeather rows, no duplicate on re-run.

Dual-ORM (Django writes seed data, fastapp reads via SQLAlchemy) → Postgres +
``transaction=True``.
"""

from __future__ import annotations

import datetime

import pytest
from django.conf import settings as dj_settings

from fastapp import tasks_compute as fp

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


def _mk_zone(django_user_model):
    from apps.irrigation.models import Zone

    u = django_user_model.objects.create_user(
        username="et0-user",
        email="et0@example.com",
        password="x",
        latitude=33.5,  # FAO-56 solar geometry needs the owner's coordinates
        longitude=-7.6,
    )
    z = Zone.objects.create(
        user=u,
        name="ET0 Zone",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
        elevation_m=120.0,
    )
    return u, z


def _seed_weather(u, z):
    """One reading per FAO-56 input in the previous-hour window (~40 min ago)."""
    from analytics.models import (
        HumidityWeather,
        PressureWeather,
        SolarRadiation,
        TemperatureWeather,
        WindSpeed,
    )

    # Mid previous-hour window [floor(now)-1h, floor(now)) so the reading is
    # always inside the bucket the task averages, whatever minute the test runs.
    floor_hour = datetime.datetime.now(datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    ts = floor_hour - datetime.timedelta(minutes=30)
    for model, value in (
        (TemperatureWeather, 25.0),
        (HumidityWeather, 50.0),
        (WindSpeed, 2.0),
        (SolarRadiation, 400.0),
        (PressureWeather, 1013.0),
    ):
        model.objects.create(user=u, zone=z, value=value, timestamp=ts)


def _et0_rows():
    from analytics.models import Et0Calculated, VPDWeather

    return (
        sorted((r.zone_id, round(r.value, 6)) for r in Et0Calculated.objects.all()),
        sorted((r.zone_id, round(r.value, 6)) for r in VPDWeather.objects.all()),
    )


def _clear_et0():
    from analytics.models import Et0Calculated, VPDWeather

    Et0Calculated.objects.all().delete()
    VPDWeather.objects.all().delete()


def test_compute_et0_vpd_hourly_identical_and_idempotent(django_user_model):
    import agriapi.tasks as dj

    u, z = _mk_zone(django_user_model)
    _seed_weather(u, z)

    dj_res = dj.compute_et0_vpd_hourly()
    dj_et0, dj_vpd = _et0_rows()
    assert dj_res["et0_rows"] == 1, dj_res
    _clear_et0()

    fp_res = fp.compute_et0_vpd_hourly()
    fp_et0, fp_vpd = _et0_rows()

    assert dj_res == fp_res
    assert dj_et0 == fp_et0
    assert dj_vpd == fp_vpd

    # idempotent: a second fastapp run keeps exactly one row per (zone, ts)
    fp.compute_et0_vpd_hourly()
    fp_et0_again, fp_vpd_again = _et0_rows()
    assert fp_et0_again == fp_et0
    assert fp_vpd_again == fp_vpd


def test_compute_et0_vpd_no_weather_is_noop(django_user_model):
    import agriapi.tasks as dj

    u, z = _mk_zone(django_user_model)  # zone with no weather readings
    dj_res = dj.compute_et0_vpd_hourly()
    _clear_et0()
    fp_res = fp.compute_et0_vpd_hourly()
    assert dj_res == fp_res
    assert dj_res["et0_rows"] == 0
    assert _et0_rows() == ([], [])
