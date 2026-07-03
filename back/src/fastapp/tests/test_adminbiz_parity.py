"""F6 golden parity: the business-admin routes — fastapp must return
byte-identical responses to the django-ninja endpoints it replaces.

Covers /admin/billing, /admin/audit, /admin/settings, /admin/kc,
/admin/monitoring and the /admin records surface (notifications /
conversations / proactive-notices / technician-grants).

Both surfaces drive the SAME committed rows + the SAME Django-minted STAFF
access token: Django via DRF's APIClient (ninja mounts at the URL root),
fastapp via Starlette's TestClient. Each route also 403s a non-staff token.

Dual-ORM: Postgres only + committed rows (fastapp reads via a separate
SQLAlchemy connection). The unmanaged admin/monitoring/technician/assistant
tables are created session-wide by back/conftest.py.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture(autouse=True)
def _clean_admin_tables(db):
    """The business-admin / monitoring / assistant tables are ``managed=False``,
    so Django's TransactionTestCase flush (``django_table_names``) does NOT
    truncate them between tests — rows would leak and dangle their user FKs.
    Clear them before each test so both surfaces see only this test's rows."""

    def _wipe():
        from apps.alerts.models import Notification
        from apps.assistant.models import AssistantConversation, ProactiveNotice
        from apps.irrigation.models import (
            AuditEvent,
            Invoice,
            LoginEvent,
            NotificationDeliveryLog,
            Plan,
            Subscription,
            SystemSetting,
            TaskRun,
            TechnicianGrant,
            TechnicianZoneGrant,
        )
        from apps.lorawan.chirpstack.models import LoraUplink

        for model in (
            TechnicianZoneGrant,
            TechnicianGrant,
            Invoice,
            Subscription,
            Plan,
            AuditEvent,
            SystemSetting,
            TaskRun,
            NotificationDeliveryLog,
            LoginEvent,
            Notification,
            AssistantConversation,
            ProactiveNotice,
            LoraUplink,
        ):
            model.objects.all().delete()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def django() -> APIClient:
    return APIClient()


@pytest.fixture
def admin(django_user_model):
    return django_user_model.objects.create_user(
        username="biz-admin",
        email="biz-admin@example.com",
        password="irrelevant-3921",
        is_staff=True,
    )


@pytest.fixture
def plain(django_user_model):
    return django_user_model.objects.create_user(
        username="biz-plain",
        email="biz-plain@example.com",
        password="irrelevant-3921",
    )


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both(fast, django, user, path, method="get", **kw):
    tok = _token(user)
    dj_kw = dict(kw)
    if "data" in kw:
        dj_kw["format"] = "json"  # DRF APIClient defaults to multipart otherwise
    dj = getattr(django, method)(path, HTTP_AUTHORIZATION=f"Bearer {tok}", **dj_kw)
    fk = {}
    if "data" in kw:
        fk["json"] = kw["data"]
    fp = getattr(fast, method)(path, headers={"Authorization": f"Bearer {tok}"}, **fk)
    return dj, fp


def _assert_identical(dj, fp, status=200):
    assert dj.status_code == status, dj.content
    assert fp.status_code == status, fp.text
    assert dj.content == fp.content


# ===========================================================================
# non-staff 403 (status only — the Django _require_admin body differs from the
# sidecar get_current_staff_user body by design; only the 403 status is a
# contract)
# ===========================================================================
_STAFF_GET_ROUTES = [
    "/admin/billing/plans",
    "/admin/billing/subscriptions",
    "/admin/billing/invoices",
    "/admin/audit",
    "/admin/settings",
    "/admin/kc",
    "/admin/monitoring/overview",
    "/admin/monitoring/tasks",
    "/admin/monitoring/deliveries",
    "/admin/monitoring/logins",
    "/admin/notifications",
    "/admin/conversations",
    "/admin/proactive-notices",
    "/admin/technician-grants",
]


@pytest.mark.parametrize("path", _STAFF_GET_ROUTES)
def test_non_staff_is_403(fast, django, plain, path):
    dj, fp = _both(fast, django, plain, path)
    assert dj.status_code == 403
    assert fp.status_code == 403


# ===========================================================================
# billing
# ===========================================================================
def _mk_plan(**over):
    from apps.irrigation.models import Plan

    payload = {
        "name": "Basic",
        "price_dh": 99.0,
        "interval": "monthly",
        "features": ["a", "b"],
        "is_active": True,
    }
    payload.update(over)
    return Plan.objects.create(**payload)


def test_billing_plans_list_identical(fast, django, admin):
    _mk_plan(name="P1")
    _mk_plan(name="P2", price_dh=199.0, features=[])
    dj, fp = _both(fast, django, admin, "/admin/billing/plans")
    _assert_identical(dj, fp)


def test_billing_plan_create_row_match(fast, django, admin):
    from apps.irrigation.models import Plan

    body = {
        "name": "Gold",
        "price_dh": 250.5,
        "interval": "yearly",
        "features": ["x"],
        "is_active": True,
    }
    tok = _token(admin)
    dj = django.post(
        "/admin/billing/plans", body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}"
    )
    fp = fast.post(
        "/admin/billing/plans", json=body, headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    # response shape identical + non-id fields equal
    assert set(dj.json()) == set(fp.json())
    assert {k: v for k, v in dj.json().items() if k != "id"} == {
        k: v for k, v in fp.json().items() if k != "id"
    }
    # both rows persisted
    assert Plan.objects.filter(name="Gold").count() == 2


def test_billing_plan_update_and_404(fast, django, admin):
    p = _mk_plan()
    body = {
        "name": "Renamed",
        "price_dh": 1.0,
        "interval": "monthly",
        "features": [],
        "is_active": False,
    }
    dj, fp = _both(
        fast, django, admin, f"/admin/billing/plans/{p.id}", method="put", data=body
    )
    _assert_identical(dj, fp)
    # 404 parity
    dj, fp = _both(
        fast,
        django,
        admin,
        "/admin/billing/plans/99999999",
        method="put",
        data=body,
    )
    _assert_identical(dj, fp, status=404)


def test_billing_subscription_create_and_list(fast, django, admin, plain):
    from apps.irrigation.models import Subscription

    p = _mk_plan()
    body = {"username": plain.username, "plan_id": p.id}
    tok = _token(admin)
    dj = django.post(
        "/admin/billing/subscriptions",
        body,
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        "/admin/billing/subscriptions",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    assert {k: v for k, v in dj.json().items() if k != "id"} == {
        k: v for k, v in fp.json().items() if k != "id"
    }
    # payement_status mirrored on the user by BOTH surfaces
    plain.refresh_from_db()
    assert plain.payement_status == "actif"
    assert Subscription.objects.filter(user=plain).count() == 2


def test_billing_subscription_user_not_found_404(fast, django, admin):
    p = _mk_plan()
    body = {"username": "nobody-here", "plan_id": p.id}
    dj, fp = _both(
        fast,
        django,
        admin,
        "/admin/billing/subscriptions",
        method="post",
        data=body,
    )
    _assert_identical(dj, fp, status=404)


def test_billing_invoice_create_and_mark_paid(fast, django, admin, plain):
    from apps.irrigation.models import Invoice, Subscription

    p = _mk_plan()
    sub = Subscription.objects.create(user=plain, plan=p, status="active")
    body = {"subscription_id": sub.id, "amount_dh": 42.0}
    tok = _token(admin)
    dj = django.post(
        "/admin/billing/invoices",
        body,
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        "/admin/billing/invoices",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    # issued_at is a volatile auto timestamp; compare the rest
    volatile = {"id", "issued_at"}
    assert {k: v for k, v in dj.json().items() if k not in volatile} == {
        k: v for k, v in fp.json().items() if k not in volatile
    }
    assert dj.json()["status"] == fp.json()["status"] == "unpaid"

    # mark-paid on the fastapp-created invoice, byte-checked structurally
    inv_fp = Invoice.objects.exclude(id=dj.json()["id"]).get()
    r = fast.post(
        f"/admin/billing/invoices/{inv_fp.id}/mark-paid",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "paid"
    assert r.json()["paid_at"] is not None


# ===========================================================================
# audit
# ===========================================================================
def _mk_audit(actor, **over):
    from apps.irrigation.models import AuditEvent

    payload = {
        "actor": actor,
        "action": "plan.create",
        "target_type": "plan",
        "target_id": "7",
        "changes": {"name": "x"},
    }
    payload.update(over)
    return AuditEvent.objects.create(**payload)


def test_audit_list_identical(fast, django, admin):
    _mk_audit(admin, action="plan.create")
    _mk_audit(admin, action="plan.delete", target_type="plan", target_id="9")
    dj, fp = _both(fast, django, admin, "/admin/audit")
    _assert_identical(dj, fp)


def test_audit_filters_identical(fast, django, admin):
    _mk_audit(admin, action="plan.create")
    _mk_audit(admin, action="invoice.create", target_type="invoice")
    dj, fp = _both(
        fast, django, admin, "/admin/audit?action=create&target_type=invoice"
    )
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, f"/admin/audit?actor={admin.username}")
    _assert_identical(dj, fp)


# ===========================================================================
# settings
# ===========================================================================
def test_settings_get_seeds_and_matches(fast, django, admin):
    # Django GET seeds the defaults; fastapp GET sees the same rows.
    tok = _token(admin)
    dj = django.get("/admin/settings", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.get("/admin/settings", headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


def test_settings_patch_and_create_and_delete(fast, django, admin):
    from apps.irrigation.models import SystemSetting

    tok = _token(admin)
    # seed first
    fast.get("/admin/settings", headers={"Authorization": f"Bearer {tok}"})

    # PATCH upsert
    dj = django.patch(
        "/admin/settings",
        {"values": {"support_email": "new@x.com", "brand": "Agro"}},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.patch(
        "/admin/settings",
        json={"values": {"support_email": "new@x.com", "brand": "Agro"}},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content

    # POST create — conflict on second identical create (409)
    dj = django.post(
        "/admin/settings",
        {"key": "feature_x", "value": True, "category": "flags"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    assert dj.status_code == 200
    fp = fast.post(
        "/admin/settings",
        json={"key": "feature_x", "value": True, "category": "flags"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert fp.status_code == 409  # already created by Django
    assert fp.json() == {"detail": "Setting already exists"}

    # DELETE + 404 parity
    SystemSetting.objects.get_or_create(key="temp_key", defaults={"value": 1})
    dj = django.delete("/admin/settings/temp_key", HTTP_AUTHORIZATION=f"Bearer {tok}")
    assert dj.status_code == 200
    fp = fast.delete(
        "/admin/settings/temp_key", headers={"Authorization": f"Bearer {tok}"}
    )
    assert fp.status_code == 404
    assert fp.json() == {"detail": "Not found"}


def test_settings_create_missing_key_400(fast, django, admin):
    dj, fp = _both(
        fast,
        django,
        admin,
        "/admin/settings",
        method="post",
        data={"key": "  ", "value": 1},
    )
    _assert_identical(dj, fp, status=400)


# ===========================================================================
# kc
# ===========================================================================
def _mk_zone(user, **over):
    from apps.irrigation.models import Zone

    payload = {
        "name": f"zone-{user.username}",
        "space": 1000.0,
        "critical_moisture_threshold": 20.0,
        "pomp_flow_rate": 1.0,
        "elevation_m": 120.0,
    }
    payload.update(over)
    return Zone.objects.create(user=user, **payload)


def _mk_kc(user, zone=None):
    from apps.irrigation.models import Kc, KcPeriod, KcPeriodAssignment

    kc = Kc.objects.create(
        user=user, zone=zone, name="Tomato", plant_name="Solanum", number_of_periods=0
    )
    for pn, s, e, v in [
        ("mid", "2026-03-01", "2026-04-01", 1.15),
        ("init", "2026-01-01", "2026-02-01", 0.6),
    ]:
        p = KcPeriod.objects.create(
            period_name=pn, start_date=s, end_date=e, kc_value=v
        )
        KcPeriodAssignment.objects.create(kc=kc, period=p)
    kc.number_of_periods = 2
    kc.save(update_fields=["number_of_periods"])
    return kc


def test_admin_kc_list_and_get_identical(fast, django, admin, plain):
    z = _mk_zone(plain)
    kc = _mk_kc(plain, zone=z)
    dj, fp = _both(fast, django, admin, "/admin/kc")
    _assert_identical(dj, fp)
    # username present + it's the owner
    assert fp.json()[0]["username"] == plain.username
    dj, fp = _both(fast, django, admin, f"/admin/kc/{kc.id}")
    _assert_identical(dj, fp)


def test_admin_kc_get_404(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/admin/kc/99999999")
    _assert_identical(dj, fp, status=404)


def test_admin_kc_create_and_row(fast, django, admin, plain):
    from apps.irrigation.models import Kc

    z = _mk_zone(plain)
    body = {
        "username": plain.username,
        "name": "Wheat",
        "plant_name": "Triticum",
        "zone_id": z.id,
        "periods": [
            {
                "period_name": "init",
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
                "kc_value": 0.4,
            }
        ],
    }
    tok = _token(admin)
    dj = django.post(
        "/admin/kc", body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}"
    )
    fp = fast.post("/admin/kc", json=body, headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == fp.status_code == 201, (dj.content, fp.text)
    assert {k: v for k, v in dj.json().items() if k not in ("id", "periods")} == {
        k: v for k, v in fp.json().items() if k not in ("id", "periods")
    }
    assert Kc.objects.filter(name="Wheat").count() == 2


def test_admin_kc_create_user_not_found_404(fast, django, admin):
    body = {"username": "ghost", "name": "X", "plant_name": "Y", "periods": []}
    dj, fp = _both(fast, django, admin, "/admin/kc", method="post", data=body)
    _assert_identical(dj, fp, status=404)


# ===========================================================================
# monitoring
# ===========================================================================
def _mk_taskrun(name, status="success", error=""):
    from apps.irrigation.models import TaskRun

    return TaskRun.objects.create(
        task_name=name, status=status, error=error, runtime_ms=12, result={}
    )


def _mk_delivery(channel="email", status="sent", user=None, kind="alert"):
    from apps.irrigation.models import NotificationDeliveryLog

    return NotificationDeliveryLog.objects.create(
        channel=channel, kind=kind, recipient="x@y.com", status=status, user=user
    )


def _mk_login(username, success=True):
    from apps.irrigation.models import LoginEvent

    return LoginEvent.objects.create(
        username=username, success=success, ip="1.2.3.4", user_agent="pytest"
    )


def _mk_uplink(dev_eui, hours_ago=1):
    from apps.lorawan.chirpstack.models import LoraUplink

    return LoraUplink.objects.create(
        dev_eui=dev_eui,
        device_name="d",
        received_at=datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=hours_ago),
    )


def test_monitoring_overview_identical(fast, django, admin):
    _mk_taskrun("task_a")
    _mk_taskrun("task_a")
    _mk_taskrun("task_b", status="failure", error="boom")
    _mk_delivery(channel="email", status="sent")
    _mk_delivery(channel="sms", status="failed", user=admin)
    _mk_login("u1", success=True)
    _mk_login("u2", success=False)
    _mk_uplink("AA01", hours_ago=1)
    _mk_uplink("AA02", hours_ago=48)
    dj, fp = _both(fast, django, admin, "/admin/monitoring/overview")
    _assert_identical(dj, fp)


def test_monitoring_tasks_static_schedule_identical(fast, django, admin):
    _mk_taskrun("task_a")
    _mk_taskrun("task_b", status="failure", error="x")
    dj, fp = _both(fast, django, admin, "/admin/monitoring/tasks")
    _assert_identical(dj, fp)
    # the static CELERY_BEAT_SCHEDULE entries are present + identical
    assert any(e["source"] == "static" for e in fp.json()["schedule"])


def test_monitoring_tasks_database_schedule_identical(fast, django, admin):
    from django_celery_beat.models import (
        CrontabSchedule,
        IntervalSchedule,
        PeriodicTask,
    )

    iv, _ = IntervalSchedule.objects.get_or_create(
        every=5, period=IntervalSchedule.MINUTES
    )
    PeriodicTask.objects.create(name="pt-interval", task="agriapi.tasks.x", interval=iv)
    cron, _ = CrontabSchedule.objects.get_or_create(
        minute="*/15", hour="*", day_of_week="*", day_of_month="*", month_of_year="*"
    )
    PeriodicTask.objects.create(name="pt-cron", task="agriapi.tasks.y", crontab=cron)
    dj, fp = _both(fast, django, admin, "/admin/monitoring/tasks")
    _assert_identical(dj, fp)
    db_entries = [e for e in fp.json()["schedule"] if e["source"] == "database"]
    assert {e["name"] for e in db_entries} >= {"pt-interval", "pt-cron"}


def test_monitoring_deliveries_and_logins_identical(fast, django, admin):
    _mk_delivery(channel="email", status="sent", user=admin)
    _mk_delivery(channel="sms", status="failed")
    _mk_login("alice", success=True)
    _mk_login("bob", success=False)
    dj, fp = _both(fast, django, admin, "/admin/monitoring/deliveries")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, "/admin/monitoring/deliveries?channel=email")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, "/admin/monitoring/logins?success=false")
    _assert_identical(dj, fp)


# ===========================================================================
# records: notifications / conversations / proactive / technician-grants
# ===========================================================================
def _mk_notification(user):
    from apps.alerts.models import Notification

    return Notification.objects.create(
        user=user,
        yesterday_temperature=Decimal("20.00"),
        today_temperature=Decimal("22.50"),
        yesterday_humidity=Decimal("50.00"),
        today_humidity=Decimal("55.00"),
        ET0=Decimal("5.00"),
        soil_humidity=Decimal("30.00"),
        soil_temperature=Decimal("18.00"),
        soil_ph=Decimal("6.50"),
        perfect_irrigation_period="06:00-08:00",
        last_irrigation_date=datetime.date(2026, 6, 1),
        last_start_irrigation_hour=datetime.time(6, 0),
        last_finish_irrigation_hour=datetime.time(8, 0),
        used_water_irrigation=Decimal("120.00"),
        notification_date=datetime.datetime(
            2026, 6, 2, 7, 30, tzinfo=datetime.timezone.utc
        ),
    )


def test_records_notifications_identical_and_delete(fast, django, admin, plain):
    n = _mk_notification(plain)
    dj, fp = _both(fast, django, admin, "/admin/notifications")
    _assert_identical(dj, fp)
    dj, fp = _both(
        fast,
        django,
        admin,
        f"/admin/notifications?username={plain.username}",
    )
    _assert_identical(dj, fp)
    # delete parity
    r = fast.delete(
        f"/admin/notifications/{n.id}",
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    assert r.status_code == 200 and r.json() == {"status": "deleted"}


def _mk_conversation(user, client_id="c1", n_msgs=2):
    from apps.assistant.models import AssistantConversation

    now = datetime.datetime.now(datetime.timezone.utc)
    return AssistantConversation.objects.create(
        user=user,
        client_id=client_id,
        title="Chat",
        messages=[{"id": i, "role": "user", "content": "hi"} for i in range(n_msgs)],
        created_at=now,
        updated_at=now,
    )


def test_records_conversations_identical(fast, django, admin, plain):
    c = _mk_conversation(plain)
    dj, fp = _both(fast, django, admin, "/admin/conversations")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, f"/admin/conversations/{c.id}")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, "/admin/conversations/99999999")
    _assert_identical(dj, fp, status=404)


def _mk_proactive(user, sent=True):
    from apps.assistant.models import ProactiveNotice

    return ProactiveNotice.objects.create(
        user=user,
        last_sent=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        if sent
        else None,
    )


def test_records_proactive_identical(fast, django, admin, plain):
    _mk_proactive(plain)
    dj, fp = _both(fast, django, admin, "/admin/proactive-notices")
    _assert_identical(dj, fp)


def _mk_grant(technician, owner, zones=()):
    from apps.irrigation.models import TechnicianGrant, TechnicianZoneGrant

    g = TechnicianGrant.objects.create(
        technician=technician, owner=owner, is_active=True
    )
    for z in zones:
        TechnicianZoneGrant.objects.create(
            grant=g, zone=z, allowed_graphs=["water_flow_status"]
        )
    return g


def test_records_technician_grants_identical(fast, django, admin, plain):
    tech = plain
    owner_z = _mk_zone(admin)
    g = _mk_grant(tech, admin, zones=[owner_z])
    dj, fp = _both(fast, django, admin, "/admin/technician-grants")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, f"/admin/technician-grants/{g.id}")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, "/admin/technician-grants/99999999")
    _assert_identical(dj, fp, status=404)


def test_records_technician_grant_patch_identical(
    fast, django, admin, plain, django_user_model
):
    # Two grants with DISTINCT (technician, owner) pairs (uniq_technician_owner).
    owner2 = django_user_model.objects.create_user(
        username="owner2", email="owner2@example.com", password="irrelevant-3921"
    )
    g_dj = _mk_grant(plain, admin)
    g_fp = _mk_grant(plain, owner2)
    tok = _token(admin)
    dj = django.patch(
        f"/admin/technician-grants/{g_dj.id}",
        {"is_active": False},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.patch(
        f"/admin/technician-grants/{g_fp.id}",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 200
    # Same response shape; is_active was toggled off on both surfaces. (owner /
    # created_at differ because the two grants are distinct rows by necessity.)
    assert set(dj.json()) == set(fp.json())
    assert dj.json()["is_active"] is False
    assert fp.json()["is_active"] is False
    assert dj.json()["scope"] == fp.json()["scope"] == []
