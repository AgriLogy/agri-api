"""F8 parity: the seven fastapp comms task bodies must behave identically to the
Django ``agriapi.tasks`` versions they replace — same return dict, same
delivery-log rows, and the same outbound message (recipient + subject + body).

Email / SMS / WhatsApp are monkeypatched on BOTH surfaces to capture the call
args instead of hitting Resend / Twilio, so the test asserts the composed
message is identical without sending anything. Both surfaces run against the
same committed rows (dual-ORM: Django writes, fastapp reads via SQLAlchemy), so
Postgres + ``transaction=True`` are required.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings

from fastapp import email as fp_email
from fastapp import sms as fp_sms
from fastapp import tasks_comms as fp

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _clean_delivery_log(db):
    """NotificationDeliveryLog is unmanaged — TransactionTestCase does not
    truncate it between tests, so wipe it or rows leak across cases."""

    def _wipe():
        from apps.irrigation.models import NotificationDeliveryLog

        NotificationDeliveryLog.objects.all().delete()

    _wipe()
    yield
    _wipe()


# --- capture harnesses ------------------------------------------------------
@pytest.fixture
def capture(monkeypatch):
    calls = {"email": [], "sms": [], "whatsapp": []}

    # fastapp side
    def _fp_email(*, api_key, from_email, to, subject, text, html=None, timeout=10):
        calls["email"].append((to[0], subject, text))
        return True

    monkeypatch.setattr(fp_email, "send_email", _fp_email)
    monkeypatch.setattr(
        fp_sms, "send_sms", lambda p, b: calls["sms"].append((p, b)) or True
    )
    monkeypatch.setattr(
        fp_sms, "send_whatsapp", lambda p, b: calls["whatsapp"].append((p, b)) or True
    )

    # Django side
    import agriapi.tasks as dj

    def _dj_send_mail(*, subject, message, from_email, recipient_list, fail_silently):
        calls["email"].append((recipient_list[0], subject, message))

    monkeypatch.setattr(dj, "send_mail", _dj_send_mail)
    import agriapi.twilio_messaging as dj_tw
    import agriapi.whatsapp as dj_wa

    monkeypatch.setattr(
        dj_tw, "send_sms", lambda p, b: calls["sms"].append((p, b)) or True
    )
    monkeypatch.setattr(
        dj_tw, "send_whatsapp", lambda p, b: calls["whatsapp"].append((p, b)) or True
    )
    monkeypatch.setattr(
        dj_wa, "send_whatsapp", lambda p, b: calls["whatsapp"].append((p, b)) or True
    )
    return calls


def _mk_user(django_user_model, **over):
    payload = dict(
        username="comms-user",
        email="comms@example.com",
        password="x",
        firstname="Co",
        phone_number="+212600001234",
    )
    payload.update(over)
    return django_user_model.objects.create_user(**payload)


def _mk_zone(user, name="Zone C"):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=user,
        name=name,
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
        elevation_m=120.0,
    )


def _mk_alert(user, zone=None, **over):
    from apps.alerts.models import Alert

    payload = dict(
        name="hi-temp",
        type="threshold",
        description="d",
        condition=">",
        condition_nbr=30,
        sensor_key="temperature_weather",
        is_active=True,
    )
    payload.update(over)
    return Alert.objects.create(user=user, zone=zone, **payload)


def _delivery_rows():
    from apps.irrigation.models import NotificationDeliveryLog

    return sorted(
        (r.channel, r.kind, r.recipient, r.status)
        for r in NotificationDeliveryLog.objects.all()
    )


TS = "2026-07-04T01:00:00+00:00"


# ===========================================================================
# send_alert_email
# ===========================================================================
def test_send_alert_email_identical(capture, django_user_model):
    import agriapi.tasks as dj
    from apps.irrigation.models import NotificationDeliveryLog

    u = _mk_user(django_user_model)
    z = _mk_zone(u)
    a = _mk_alert(u, zone=z)

    dj_res = dj.send_alert_email(alert_id=a.id, value=42.0, timestamp_iso=TS)
    dj_calls = list(capture["email"])
    dj_rows = _delivery_rows()
    NotificationDeliveryLog.objects.all().delete()
    capture["email"].clear()

    fp_res = fp.send_alert_email(alert_id=a.id, value=42.0, timestamp_iso=TS)
    fp_calls = list(capture["email"])
    fp_rows = _delivery_rows()

    assert dj_res == fp_res == {"sent": 1, "alert_id": a.id}
    assert dj_calls == fp_calls  # same recipient + subject + body
    assert dj_rows == fp_rows  # same delivery-log row


def test_send_alert_email_inactive_skips(capture, django_user_model):
    import agriapi.tasks as dj

    u = _mk_user(django_user_model)
    a = _mk_alert(u, is_active=False)
    assert (
        dj.send_alert_email(alert_id=a.id, value=1.0, timestamp_iso=TS)
        == fp.send_alert_email(alert_id=a.id, value=1.0, timestamp_iso=TS)
        == {"sent": 0, "reason": "alert_inactive"}
    )
    assert capture["email"] == []


def test_send_alert_email_missing_skips(capture, django_user_model):
    import agriapi.tasks as dj

    _mk_user(django_user_model)  # ensure a table
    assert (
        dj.send_alert_email(alert_id=999999, value=1.0, timestamp_iso=TS)
        == fp.send_alert_email(alert_id=999999, value=1.0, timestamp_iso=TS)
        == {"sent": 0, "reason": "alert_missing"}
    )


def test_send_alert_email_no_recipient(capture, django_user_model):
    import agriapi.tasks as dj

    u = _mk_user(django_user_model)
    django_user_model.objects.filter(pk=u.pk).update(email="")  # bypass manager
    a = _mk_alert(u)
    assert (
        dj.send_alert_email(alert_id=a.id, value=1.0, timestamp_iso=TS)
        == fp.send_alert_email(alert_id=a.id, value=1.0, timestamp_iso=TS)
        == {"sent": 0, "reason": "no_recipient"}
    )


# ===========================================================================
# send_alert_whatsapp / send_alert_sms
# ===========================================================================
def test_send_alert_whatsapp_identical(capture, django_user_model):
    import agriapi.tasks as dj

    u = _mk_user(django_user_model)
    z = _mk_zone(u)
    a = _mk_alert(u, zone=z)
    dj_res = dj.send_alert_whatsapp(alert_id=a.id, value=42.0, timestamp_iso=TS)
    dj_calls = list(capture["whatsapp"])
    capture["whatsapp"].clear()
    fp_res = fp.send_alert_whatsapp(alert_id=a.id, value=42.0, timestamp_iso=TS)
    fp_calls = list(capture["whatsapp"])
    assert dj_res == fp_res == {"sent": 1, "alert_id": a.id}
    assert dj_calls == fp_calls  # same phone + body


def test_send_alert_sms_identical(capture, django_user_model):
    import agriapi.tasks as dj

    u = _mk_user(django_user_model)
    z = _mk_zone(u)
    a = _mk_alert(u, zone=z)
    dj_res = dj.send_alert_sms(alert_id=a.id, value=42.0, timestamp_iso=TS)
    dj_calls = list(capture["sms"])
    capture["sms"].clear()
    fp_res = fp.send_alert_sms(alert_id=a.id, value=42.0, timestamp_iso=TS)
    fp_calls = list(capture["sms"])
    assert dj_res == fp_res == {"sent": 1, "alert_id": a.id}
    assert dj_calls == fp_calls


def test_send_alert_sms_no_phone(capture, django_user_model):
    import agriapi.tasks as dj

    u = _mk_user(django_user_model, phone_number="")
    a = _mk_alert(u)
    assert (
        dj.send_alert_sms(alert_id=a.id, value=1.0, timestamp_iso=TS)
        == fp.send_alert_sms(alert_id=a.id, value=1.0, timestamp_iso=TS)
        == {"sent": 0, "reason": "no_phone"}
    )


# ===========================================================================
# zone-outbound
# ===========================================================================
def test_send_zone_outbound_email_identical(capture, django_user_model):
    import agriapi.tasks as dj
    from apps.irrigation.models import NotificationDeliveryLog

    dj_res = dj.send_zone_outbound_email(
        recipient="to@x.com", subject="Sub", message="Hello"
    )
    dj_calls = list(capture["email"])
    dj_rows = _delivery_rows()
    NotificationDeliveryLog.objects.all().delete()
    capture["email"].clear()

    fp_res = fp.send_zone_outbound_email(
        recipient="to@x.com", subject="Sub", message="Hello"
    )
    assert dj_res == fp_res == {"sent": 1, "recipient": "to@x.com"}
    assert dj_calls == list(capture["email"])
    assert dj_rows == _delivery_rows()


def test_send_zone_outbound_sms_identical(capture, django_user_model):
    import agriapi.tasks as dj
    from apps.irrigation.models import NotificationDeliveryLog

    dj_res = dj.send_zone_outbound_sms(to_phone="+212611112222", body="Hi")
    dj_rows = _delivery_rows()
    NotificationDeliveryLog.objects.all().delete()
    capture["sms"].clear()
    fp_res = fp.send_zone_outbound_sms(to_phone="+212611112222", body="Hi")
    assert dj_res == fp_res == {"sent": 1, "recipient": "+212611112222"}
    assert dj_rows == _delivery_rows()


def test_send_zone_outbound_no_recipient(capture):
    import agriapi.tasks as dj

    assert (
        dj.send_zone_outbound_email(recipient="", subject="s", message="m")
        == fp.send_zone_outbound_email(recipient="", subject="s", message="m")
        == {"sent": 0, "reason": "no_recipient"}
    )
