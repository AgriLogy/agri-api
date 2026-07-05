"""F8b parity: ``scan_proactive_insights`` — the fastapp port must scan the same
customers (active, non-staff, non-technician), emit an irrigation nudge only for
"irrigate" advice, dedup once per cooldown window (atomic claim on
AssistantProactiveNotice.last_sent), and count scanned/notified/quiet/skipped
identically.

The irrigation-advice tool is mocked on both surfaces (it's exercised by the
assistant parity suite); email is captured; the customer filter + dedup claim
run for real. The claim mutates last_sent, reset between runs.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings

from fastapp import tasks_scan as fp

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


def _advice_for(user):
    if getattr(user, "email", "") == "irr@x.com":
        return {
            "recommendation": "irrigate",
            "zone_name": "Zone A",
            "reason": "Sol sec.",
            "estimated_water_m3": 5.0,
        }
    return {"recommendation": "hold", "reason": "RAS."}


@pytest.fixture(autouse=True)
def _clean_unmanaged(db):
    def _wipe():
        from apps.assistant.models import ProactiveNotice
        from apps.irrigation.models import NotificationDeliveryLog

        NotificationDeliveryLog.objects.all().delete()
        ProactiveNotice.objects.all().delete()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def capture(monkeypatch):
    calls = {"email": []}
    import apps.assistant.tools as dj_tools
    import fastapp.assistant.tools as fp_tools

    monkeypatch.setattr(fp_tools, "_get_irrigation_advice", lambda u, p: _advice_for(u))
    monkeypatch.setattr(dj_tools, "_get_irrigation_advice", lambda u, p: _advice_for(u))
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


def _mk_user(django_user_model, uname, email, **over):
    payload = dict(username=uname, email=email, password="x")
    payload.update(over)
    return django_user_model.objects.create_user(**payload)


def _delivery_rows():
    from apps.irrigation.models import NotificationDeliveryLog

    return sorted(
        (r.channel, r.kind, r.recipient, r.status)
        for r in NotificationDeliveryLog.objects.all()
    )


def test_scan_proactive_insights_identical(capture, django_user_model):
    import agriapi.tasks as dj
    from apps.assistant.models import ProactiveNotice
    from apps.irrigation.models import NotificationDeliveryLog

    _mk_user(django_user_model, "irr", "irr@x.com")  # → irrigate → notified
    _mk_user(django_user_model, "hold", "hold@x.com")  # → hold → quiet
    cnoemail = _mk_user(django_user_model, "noem", "noem@x.com")  # no email → skipped
    django_user_model.objects.filter(pk=cnoemail.pk).update(email="")
    _mk_user(django_user_model, "staff", "staff@x.com", is_staff=True)  # not scanned
    _mk_user(django_user_model, "tech", "tech@x.com", is_technician=True)  # not scanned

    dj_res = dj.scan_proactive_insights()
    dj_calls = sorted(capture["email"])
    dj_rows = _delivery_rows()

    ProactiveNotice.objects.all().delete()  # reset the claim
    NotificationDeliveryLog.objects.all().delete()
    capture["email"].clear()

    fp_res = fp.scan_proactive_insights()
    fp_calls = sorted(capture["email"])
    fp_rows = _delivery_rows()

    assert (
        dj_res
        == fp_res
        == {
            "scanned": 3,
            "notified": 1,
            "quiet": 1,
            "skipped": 1,
        }
    )
    assert dj_calls == fp_calls
    assert fp_calls[0][0] == "irr@x.com"
    assert dj_rows == fp_rows == [("email", "proactive", "irr@x.com", "sent")]


def test_scan_proactive_dedup_cooldown(capture, django_user_model):
    """A second scan inside the cooldown window doesn't re-notify."""
    _mk_user(django_user_model, "irr", "irr@x.com")
    assert fp.scan_proactive_insights()["notified"] == 1
    r2 = fp.scan_proactive_insights()
    assert r2["notified"] == 0
    assert r2["skipped"] == 1
