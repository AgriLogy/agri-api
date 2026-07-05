"""F8b parity: ``send_periodic_notifications`` — the cadence-gated field-status
digest. The fastapp port must gate + count identically to the Django task: only
due active users with an email are sent, the cadence slot is claimed atomically
(last_notified stamped), and the same delivery rows are written.

Message composition is mocked on both surfaces (agri-core composer is tested
elsewhere) and email is captured; the atomic cadence claim runs for real on
both (Django ORM UPDATE vs SQLAlchemy UPDATE) — that's the parity under test.
Both mutate last_notified, so it's reset between the two runs.
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
    calls = {"email": []}
    # fastapp: mock composer + email
    monkeypatch.setattr(
        fp, "compose_notification_for_user", lambda s, uid, now=None: "BODY"
    )
    monkeypatch.setattr(
        fp,
        "_send_email",
        lambda *, to, subject, body: calls["email"].append((to, subject, body)) or True,
    )
    # Django: mock composer + email
    import agriapi.tasks as dj

    monkeypatch.setattr(dj, "perform_calculations", lambda user: "BODY")

    def _dj_send_mail(*, subject, message, from_email, recipient_list, **k):
        calls["email"].append((recipient_list[0], subject, message))

    monkeypatch.setattr(dj, "send_mail", _dj_send_mail)
    return calls


def _mk_user(django_user_model, uname, **over):
    payload = dict(username=uname, email=f"{uname}@x.com", password="x", firstname="Fa")
    payload.update(over)
    return django_user_model.objects.create_user(**payload)


def _delivery_rows():
    from apps.irrigation.models import NotificationDeliveryLog

    return sorted(
        (r.channel, r.kind, r.recipient, r.status)
        for r in NotificationDeliveryLog.objects.all()
    )


def test_send_periodic_notifications_identical(capture, django_user_model):
    import agriapi.tasks as dj
    from apps.irrigation.models import NotificationDeliveryLog

    now = datetime.datetime.now(datetime.timezone.utc)
    # A: due (never notified). B: not due (notified 5 min ago, cadence 240).
    # C: active but no email. D: inactive (excluded).
    a = _mk_user(django_user_model, "due", notify_every=240)  # last_notified=None
    b = _mk_user(django_user_model, "notdue", notify_every=240)
    django_user_model.objects.filter(pk=b.pk).update(
        last_notified=now - datetime.timedelta(minutes=5)
    )
    c = _mk_user(django_user_model, "noemail", notify_every=240)
    django_user_model.objects.filter(pk=c.pk).update(email="")
    _mk_user(django_user_model, "inactive", notify_every=240, is_active=False)

    dj_res = dj.send_periodic_notifications()
    dj_calls = sorted(capture["email"])
    dj_rows = _delivery_rows()

    # reset mutated state for the fastapp run
    django_user_model.objects.filter(pk=a.pk).update(last_notified=None)
    django_user_model.objects.filter(pk=b.pk).update(
        last_notified=now - datetime.timedelta(minutes=5)
    )
    NotificationDeliveryLog.objects.all().delete()
    capture["email"].clear()

    fp_res = fp.send_periodic_notifications()
    fp_calls = sorted(capture["email"])
    fp_rows = _delivery_rows()

    assert dj_res == fp_res == {"sent": 1, "skipped": 2, "failed": 0}
    assert dj_calls == fp_calls
    assert dj_calls[0][0] == "due@x.com"
    assert dj_rows == fp_rows == [("email", "periodic", "due@x.com", "sent")]
    # the due user's slot was claimed (last_notified stamped)
    assert django_user_model.objects.get(pk=a.pk).last_notified is not None


def test_send_periodic_notifications_none_due(capture, django_user_model):
    import agriapi.tasks as dj

    now = datetime.datetime.now(datetime.timezone.utc)
    u = _mk_user(django_user_model, "recent", notify_every=240)
    django_user_model.objects.filter(pk=u.pk).update(
        last_notified=now - datetime.timedelta(minutes=1)
    )
    dj_res = dj.send_periodic_notifications()
    fp_res = fp.send_periodic_notifications()
    assert dj_res == fp_res == {"sent": 0, "skipped": 1, "failed": 0}
    assert capture["email"] == []
