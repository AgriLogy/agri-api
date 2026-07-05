"""F8b parity: ``scan_device_health`` — the fastapp port must classify + notify
identically to the Django task: offline / low-battery devices email their owner
once per cooldown window (atomic dedup claim on last_health_notified), healthy
and no-recipient devices are counted but not emailed.

Email is captured on both surfaces; the health classification + dedup claim run
for real (Django ORM vs SQLAlchemy). The claim mutates last_health_notified, so
it's reset between the two runs.
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
        from apps.irrigation.models import Device, NotificationDeliveryLog
        from apps.lorawan.chirpstack.models import LoraUplink

        NotificationDeliveryLog.objects.all().delete()
        LoraUplink.objects.all().delete()
        Device.objects.all().delete()  # unmanaged — not truncated between tests

    _wipe()
    yield
    _wipe()


@pytest.fixture
def capture(monkeypatch):
    calls = {"email": []}
    monkeypatch.setattr(
        fp,
        "_send_email",
        lambda *, to, subject, body: calls["email"].append((to, subject, body)) or True,
    )
    import agriapi.tasks as dj

    def _dj_send_mail(*, subject, message, from_email, recipient_list, **k):
        calls["email"].append((recipient_list[0], subject, message))

    monkeypatch.setattr(dj, "send_mail", _dj_send_mail)
    return calls


def _mk_device(django_user_model, uname, serial, name, email="d@x.com"):
    from apps.irrigation.models import Device

    u = django_user_model.objects.create_user(username=uname, email=email, password="x")
    d = Device.objects.create(
        user=u, device_type="bivocom", serial=serial, name=name, is_active=True
    )
    return u, d


def _uplink(serial, hours_ago, battery_v):
    from apps.lorawan.chirpstack.models import LoraUplink

    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=hours_ago
    )
    LoraUplink.objects.create(
        dev_eui=serial, device_name=serial, received_at=ts, battery_v=battery_v
    )


def _delivery_rows():
    from apps.irrigation.models import NotificationDeliveryLog

    return sorted(
        (r.channel, r.kind, r.recipient, r.status)
        for r in NotificationDeliveryLog.objects.all()
    )


def _reset_claims():
    from apps.irrigation.models import Device

    Device.objects.all().update(last_health_notified=None)


def test_scan_device_health_identical(capture, django_user_model):
    import agriapi.tasks as dj
    from apps.irrigation.models import Device, NotificationDeliveryLog

    # offline (uplink 30h old) → notified
    _u1, d1 = _mk_device(django_user_model, "off-u", "AA01", "Offline Dev", "off@x.com")
    _uplink("AA01", hours_ago=30, battery_v=3.7)
    # healthy (recent + good battery)
    _u2, d2 = _mk_device(django_user_model, "ok-u", "BB02", "OK Dev", "ok@x.com")
    _uplink("BB02", hours_ago=1, battery_v=3.7)
    # low battery (recent but < 3.4) → notified
    _u3, d3 = _mk_device(django_user_model, "lb-u", "CC03", "LowBat Dev", "lb@x.com")
    _uplink("CC03", hours_ago=1, battery_v=3.0)
    # offline + owner has no email → skipped
    u4, d4 = _mk_device(django_user_model, "noem-u", "DD04", "NoMail Dev")
    django_user_model.objects.filter(pk=u4.pk).update(email="")
    _uplink("DD04", hours_ago=30, battery_v=3.7)

    dj_res = dj.scan_device_health()
    dj_calls = sorted(capture["email"])
    dj_rows = _delivery_rows()

    _reset_claims()
    NotificationDeliveryLog.objects.all().delete()
    capture["email"].clear()

    fp_res = fp.scan_device_health()
    fp_calls = sorted(capture["email"])
    fp_rows = _delivery_rows()

    assert (
        dj_res
        == fp_res
        == {
            "scanned": 4,
            "notified": 2,
            "healthy": 1,
            "skipped": 1,
        }
    )
    assert dj_calls == fp_calls
    assert {c[0] for c in fp_calls} == {"off@x.com", "lb@x.com"}
    assert dj_rows == fp_rows
    # both notified devices had their cooldown claimed
    assert Device.objects.get(pk=d1.pk).last_health_notified is not None
    assert Device.objects.get(pk=d3.pk).last_health_notified is not None


def test_scan_device_health_dedup_cooldown(capture, django_user_model):
    """A second scan inside the cooldown window doesn't re-notify."""
    _u, _d = _mk_device(django_user_model, "off-u", "AA01", "Offline Dev", "off@x.com")
    _uplink("AA01", hours_ago=30, battery_v=3.7)

    assert fp.scan_device_health()["notified"] == 1
    r2 = fp.scan_device_health()
    assert r2["notified"] == 0
    assert r2["skipped"] == 1  # claim already held


def test_classify_device_health_pure():
    ref = datetime.datetime(2026, 7, 5, 12, 0, tzinfo=datetime.timezone.utc)
    assert fp.classify_device_health(None, 3.7, reference=ref) == ["offline"]
    fresh = ref - datetime.timedelta(hours=1)
    assert fp.classify_device_health(fresh, 3.7, reference=ref) == []
    assert fp.classify_device_health(fresh, 3.0, reference=ref) == ["low_battery"]
    old = ref - datetime.timedelta(hours=30)
    assert fp.classify_device_health(old, 3.0, reference=ref) == [
        "offline",
        "low_battery",
    ]
