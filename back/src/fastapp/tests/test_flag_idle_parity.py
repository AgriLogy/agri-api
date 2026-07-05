"""F8b parity: ``flag_idle_zones`` — the fastapp port must flag the same zones
as the Django task (a zone that went silent past ZONE_IDLE_THRESHOLD_HOURS),
skipping fresh and never-reported zones, and emit the same email + delivery row.

The reflag throttle differs by backend (Django LocMem ``cache.add`` vs fastapp
Redis ``SET NX EX``), so both are mocked to always-claim for the flagging-parity
tests; a separate test exercises the fastapp throttle's claim-once semantics
with an in-memory stand-in (no Redis needed).
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


@pytest.fixture(autouse=True)
def _clean_delivery_log(db):
    def _wipe():
        from apps.irrigation.models import NotificationDeliveryLog

        NotificationDeliveryLog.objects.all().delete()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def capture(monkeypatch):
    """Always-claim the reflag slot + capture emails on both surfaces."""
    calls = {"email": []}

    # fastapp
    monkeypatch.setattr(fp, "claim_reflag_slot", lambda zid, ttl: True)
    monkeypatch.setattr(
        fp,
        "_send_email",
        lambda *, to, subject, body: calls["email"].append((to, subject, body)) or True,
    )
    # Django
    import agriapi.tasks as dj
    from django.core.cache import cache

    monkeypatch.setattr(cache, "add", lambda *a, **k: True)

    def _dj_send_mail(*, subject, message, from_email, recipient_list, fail_silently):
        calls["email"].append((recipient_list[0], subject, message))

    monkeypatch.setattr(dj, "send_mail", _dj_send_mail)
    return calls


def _mk_zone(django_user_model, uname, zname, email="z@example.com"):
    from apps.irrigation.models import Zone

    u = django_user_model.objects.create_user(
        username=uname, email=email, password="x", firstname="Fa"
    )
    z = Zone.objects.create(
        user=u,
        name=zname,
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
        elevation_m=120.0,
    )
    return u, z


def _reading(u, z, hours_ago):
    from analytics.models import TemperatureWeather

    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=hours_ago
    )
    TemperatureWeather.objects.create(user=u, zone=z, value=25.0, timestamp=ts)


def _delivery_rows():
    from apps.irrigation.models import NotificationDeliveryLog

    return sorted(
        (r.channel, r.kind, r.recipient, r.status)
        for r in NotificationDeliveryLog.objects.all()
    )


def test_flag_idle_zones_identical(capture, django_user_model):
    import agriapi.tasks as dj
    from apps.irrigation.models import NotificationDeliveryLog

    # idle (30h old), fresh (1h old), never-reported
    ui, zi = _mk_zone(django_user_model, "idle-u", "Idle Zone", "idle@x.com")
    _reading(ui, zi, hours_ago=30)
    uf, zf = _mk_zone(django_user_model, "fresh-u", "Fresh Zone", "fresh@x.com")
    _reading(uf, zf, hours_ago=1)
    _mk_zone(django_user_model, "never-u", "Never Zone", "never@x.com")

    dj_res = dj.flag_idle_zones()
    dj_calls = sorted(capture["email"])
    dj_rows = _delivery_rows()
    NotificationDeliveryLog.objects.all().delete()
    capture["email"].clear()

    fp_res = fp.flag_idle_zones()
    fp_calls = sorted(capture["email"])
    fp_rows = _delivery_rows()

    assert dj_res == fp_res == {"flagged": 1}
    assert dj_calls == fp_calls  # same recipient + subject + body (only the idle zone)
    assert dj_calls[0][0] == "idle@x.com"
    assert dj_rows == fp_rows == [("email", "liveness", "idle@x.com", "sent")]


def test_flag_idle_zones_none_idle(capture, django_user_model):
    import agriapi.tasks as dj

    uf, zf = _mk_zone(django_user_model, "fresh-u", "Fresh Zone")
    _reading(uf, zf, hours_ago=1)
    assert dj.flag_idle_zones() == fp.flag_idle_zones() == {"flagged": 0}
    assert capture["email"] == []


def test_flag_idle_no_recipient_consumes_slot_no_email(capture, django_user_model):
    """A no-recipient idle zone claims its slot but sends nothing — flagged 0."""
    import agriapi.tasks as dj

    ui, zi = _mk_zone(django_user_model, "idle-u", "Idle Zone")
    django_user_model.objects.filter(pk=ui.pk).update(email="")
    _reading(ui, zi, hours_ago=30)
    assert dj.flag_idle_zones() == fp.flag_idle_zones() == {"flagged": 0}
    assert capture["email"] == []
    assert _delivery_rows() == []


def test_fastapp_reflag_throttle_claims_once(monkeypatch, django_user_model):
    """The fastapp throttle only flags once per window: with an in-memory claim
    stand-in, a second run in the same window flags nothing."""
    claimed: set[int] = set()

    def _claim(zid, ttl):
        if zid in claimed:
            return False
        claimed.add(zid)
        return True

    monkeypatch.setattr(fp, "claim_reflag_slot", _claim)
    monkeypatch.setattr(fp, "_send_email", lambda **k: True)

    ui, zi = _mk_zone(django_user_model, "idle-u", "Idle Zone", "idle@x.com")
    _reading(ui, zi, hours_ago=30)

    assert fp.flag_idle_zones() == {"flagged": 1}
    assert fp.flag_idle_zones() == {"flagged": 0}  # slot already claimed
