"""F6-admin-rest golden parity: the remaining admin routers — fastapp must
return byte-identical responses to the django-ninja endpoints it replaces.

Covers the analytics admin tree (``apps/irrigation/router_admin.py``): overview,
business analytics, device-fleet health, per-user zones/params/active-graph/
graph-config, the alerts console + override, activity timeline, sensor-units;
the ``/admin/sensor-data`` explorer (``apps/sensors/router_sensor_data.py``);
the ``/admin`` backfill (``apps/irrigation/router_backfill.py``); and read-only
``/admin/impersonate`` (``apps/irrigation/router_impersonation.py``).

Also asserts the ``/admin/monitoring`` overview+tasks byte-parity fix for a
DatabaseScheduler *solar* schedule (previously ``sunrise`` vs ``Sunrise``).

Both surfaces drive the SAME committed rows + the SAME Django-minted STAFF
access token: Django via DRF's APIClient (ninja mounts at the URL root),
fastapp via Starlette's TestClient. Reads + 404 envelopes are byte-checked;
timestamped writes + the minted impersonation token are checked structurally.
"""

from __future__ import annotations

import datetime

import jwt
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
def _clean_unmanaged(db):
    """LoRaWAN uplinks + celery-beat + admin tables are unmanaged / not flushed
    by TransactionTestCase — clear them so both surfaces see only this test."""

    def _wipe():
        from apps.lorawan.chirpstack.models import LoraUplink

        LoraUplink.objects.all().delete()
        try:
            from django_celery_beat.models import PeriodicTask

            PeriodicTask.objects.all().delete()
        except Exception:
            pass

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
        username="rest-admin",
        email="rest-admin@example.com",
        password="irrelevant-3921",
        is_staff=True,
    )


@pytest.fixture
def plain(django_user_model):
    return django_user_model.objects.create_user(
        username="rest-plain",
        email="rest-plain@example.com",
        password="irrelevant-3921",
    )


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both(fast, django, user, path, method="get", **kw):
    tok = _token(user)
    dj_kw = dict(kw)
    if "data" in kw:
        dj_kw["format"] = "json"
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


# --- factories -------------------------------------------------------------
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


def _mk_alert(user, zone=None, **over):
    from apps.alerts.models import Alert

    payload = {
        "name": "hi-temp",
        "type": "threshold",
        "description": "desc",
        "condition": ">",
        "condition_nbr": 30,
        "sensor_key": "temperatureweather",
        "is_active": True,
    }
    payload.update(over)
    return Alert.objects.create(user=user, zone=zone, **payload)


def _mk_reading(model, user, zone, value, ts):
    return model.objects.create(user=user, zone=zone, value=value, timestamp=ts)


def _mk_uplink(dev_eui, hours_ago=1, **over):
    from apps.lorawan.chirpstack.models import LoraUplink

    payload = dict(
        dev_eui=dev_eui,
        device_name="dev",
        received_at=datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=hours_ago),
        battery_v=3.7,
        rssi=-90.0,
        snr=8.5,
    )
    payload.update(over)
    return LoraUplink.objects.create(**payload)


# ===========================================================================
# non-staff 403
# ===========================================================================
_STAFF_GET_ROUTES = [
    "/admin/overview",
    "/admin/analytics",
    "/admin/devices/health",
    "/admin/alerts",
    "/admin/alert-analytics",
    "/admin/sensor-data/catalog",
    "/admin/sensor-data?sensor=temperatureweather",
]


@pytest.mark.parametrize("path", _STAFF_GET_ROUTES)
def test_non_staff_is_403(fast, django, plain, path):
    dj, fp = _both(fast, django, plain, path)
    assert dj.status_code == 403
    assert fp.status_code == 403


def test_impersonate_non_staff_403(fast, django, plain):
    dj, fp = _both(
        fast, django, plain, f"/admin/impersonate/{plain.username}", method="post"
    )
    assert dj.status_code == 403
    assert fp.status_code == 403


# ===========================================================================
# overview + analytics + device health
# ===========================================================================
def test_overview_identical(fast, django, admin, plain):
    _mk_zone(plain)
    a = _mk_alert(plain)
    a.last_triggered_at = datetime.datetime.now(datetime.timezone.utc)
    a.save(update_fields=["last_triggered_at"])
    dj, fp = _both(fast, django, admin, "/admin/overview")
    _assert_identical(dj, fp)


def test_analytics_identical(fast, django, admin, plain, django_user_model):
    # customers with distinct payment statuses + zones + alerts.
    plain.payement_status = "actif"
    plain.save(update_fields=["payement_status"])
    c2 = django_user_model.objects.create_user(
        username="cust2", email="c2@x.com", password="x", payement_status="suspended"
    )
    _mk_zone(plain)
    _mk_zone(plain, name="z2")
    _mk_zone(c2)
    _mk_alert(plain, type="threshold")
    _mk_alert(c2, type="anomaly")
    _mk_uplink("AA01", hours_ago=1)
    _mk_uplink("AA02", hours_ago=48)
    dj, fp = _both(fast, django, admin, "/admin/analytics")
    _assert_identical(dj, fp)


def test_device_health_identical(fast, django, admin):
    _mk_uplink("AA01", hours_ago=1)
    _mk_uplink("AA01", hours_ago=2)
    _mk_uplink("BB02", hours_ago=48)
    _mk_uplink("CC03", hours_ago=100)
    dj, fp = _both(fast, django, admin, "/admin/devices/health")
    _assert_identical(dj, fp)


def test_device_health_empty_identical(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/admin/devices/health")
    _assert_identical(dj, fp)


# ===========================================================================
# zones per user
# ===========================================================================
def test_zones_list_get_params_identical(fast, django, admin, plain):
    z = _mk_zone(plain, space=500.0, soil_param_FC=30.0, soil_param_WP=10.0)
    dj, fp = _both(fast, django, admin, f"/users/{plain.username}/zones")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, f"/users/{plain.username}/zones/{z.id}")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, f"/users/{plain.username}/zones/{z.id}/params")
    _assert_identical(dj, fp)


def test_zones_user_not_found_404(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/users/ghost/zones")
    _assert_identical(dj, fp, status=404)
    dj, fp = _both(fast, django, admin, "/users/ghost/zones/1")
    _assert_identical(dj, fp, status=404)


def test_zone_not_found_404(fast, django, admin, plain):
    dj, fp = _both(fast, django, admin, f"/users/{plain.username}/zones/99999999")
    _assert_identical(dj, fp, status=404)


def test_zone_create_row_match(fast, django, admin, plain):
    from apps.irrigation.models import Zone

    body = {
        "name": "new-zone",
        "space": 800.0,
        "soil_param_TAW": 100.0,
        "soil_param_FC": 40.0,
        "soil_param_WP": 15.0,
        "soil_param_RAW": 60.0,
        "critical_moisture_threshold": 25.0,
        "pomp_flow_rate": 2.0,
        "irrigation_water_quantity": 5.0,
        "elevation_m": 200.0,
    }
    tok = _token(admin)
    dj = django.post(
        f"/users/{plain.username}/zones",
        body,
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        f"/users/{plain.username}/zones",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 201, (dj.content, fp.text)
    assert {k: v for k, v in dj.json().items() if k != "id"} == {
        k: v for k, v in fp.json().items() if k != "id"
    }
    assert Zone.objects.filter(name="new-zone").count() == 2


def test_zone_create_validation_400(fast, django, admin, plain):
    dj, fp = _both(
        fast,
        django,
        admin,
        f"/users/{plain.username}/zones",
        method="post",
        data={"name": "bad", "space": -1.0},
    )
    _assert_identical(dj, fp, status=400)


def test_zone_update_identical(fast, django, admin, plain):
    z = _mk_zone(plain)
    body = {"critical_moisture_threshold": 55.0}
    dj, fp = _both(
        fast,
        django,
        admin,
        f"/users/{plain.username}/zones/{z.id}",
        method="patch",
        data=body,
    )
    # zone has no timestamps → the serialized row is fully deterministic.
    _assert_identical(dj, fp)


# ===========================================================================
# active-graph / graph-names / sensor-colors (get_or_create → Django-first)
# ===========================================================================
def test_active_graph_get_identical(fast, django, admin, plain):
    z = _mk_zone(plain)
    dj, fp = _both(
        fast, django, admin, f"/users/{plain.username}/zones/{z.id}/active-graph"
    )
    _assert_identical(dj, fp)


def test_graph_names_get_identical(fast, django, admin, plain):
    z = _mk_zone(plain)
    dj, fp = _both(
        fast, django, admin, f"/users/{plain.username}/zones/{z.id}/graph-names"
    )
    _assert_identical(dj, fp)


def test_sensor_colors_get_identical(fast, django, admin, plain):
    z = _mk_zone(plain)
    dj, fp = _both(
        fast, django, admin, f"/users/{plain.username}/zones/{z.id}/sensor-colors"
    )
    _assert_identical(dj, fp)


# ===========================================================================
# alerts console
# ===========================================================================
def test_user_alerts_list_identical(fast, django, admin, plain):
    z = _mk_zone(plain)
    _mk_alert(plain, zone=z, name="a1")
    _mk_alert(plain, name="a2", condition_nbr=12.5)
    dj, fp = _both(fast, django, admin, f"/users/{plain.username}/alerts")
    _assert_identical(dj, fp)


def test_alerts_get_and_404(fast, django, admin, plain):
    a = _mk_alert(plain)
    dj, fp = _both(fast, django, admin, f"/admin/alerts/{a.id}")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, "/admin/alerts/99999999")
    _assert_identical(dj, fp, status=404)


def test_alerts_list_filtered_identical(fast, django, admin, plain):
    z = _mk_zone(plain)
    _mk_alert(plain, zone=z, name="a1", type="threshold")
    _mk_alert(plain, name="a2", type="anomaly")
    dj, fp = _both(fast, django, admin, "/admin/alerts")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, "/admin/alerts?type=threshold")
    _assert_identical(dj, fp)
    dj, fp = _both(
        fast, django, admin, f"/admin/alerts?username={plain.username}&is_active=true"
    )
    _assert_identical(dj, fp)


def test_alert_analytics_identical(fast, django, admin, plain):
    a1 = _mk_alert(plain, name="a1", type="threshold", sensor_key="temperatureweather")
    _mk_alert(plain, name="a2", type="threshold", sensor_key="phsoil")
    _mk_alert(plain, name="a3", type="anomaly", sensor_key="temperatureweather")
    a1.last_triggered_at = datetime.datetime.now(datetime.timezone.utc)
    a1.save(update_fields=["last_triggered_at"])
    dj, fp = _both(fast, django, admin, "/admin/alert-analytics")
    _assert_identical(dj, fp)


def test_alert_create_row_match(fast, django, admin, plain):
    from apps.alerts.models import Alert

    z = _mk_zone(plain)
    body = {
        "username": plain.username,
        "name": "created-alert",
        "type": "threshold",
        "condition": ">",
        "condition_nbr": 42.0,
        "sensor_key": "temperatureweather",
        "description": "d",
        "zone_id": z.id,
        "is_active": True,
    }
    tok = _token(admin)
    dj = django.post(
        "/admin/alerts", body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}"
    )
    fp = fast.post(
        "/admin/alerts", json=body, headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    volatile = {"id", "created_at", "updated_at"}
    assert {k: v for k, v in dj.json().items() if k not in volatile} == {
        k: v for k, v in fp.json().items() if k not in volatile
    }
    assert Alert.objects.filter(name="created-alert").count() == 2


def test_alert_create_user_not_found_404(fast, django, admin):
    body = {
        "username": "ghost",
        "name": "x",
        "type": "t",
        "condition": ">",
        "condition_nbr": 1.0,
    }
    dj, fp = _both(fast, django, admin, "/admin/alerts", method="post", data=body)
    _assert_identical(dj, fp, status=404)


def test_alert_patch_structural(fast, django, admin, plain):
    a_dj = _mk_alert(plain, name="pa")
    tok = _token(admin)
    dj = django.patch(
        f"/admin/alerts/{a_dj.id}",
        {"is_active": False},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.patch(
        f"/admin/alerts/{a_dj.id}",
        json={"is_active": True},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 200
    assert set(dj.json()) == set(fp.json())


# ===========================================================================
# activity + sensor-units
# ===========================================================================
def test_activity_identical(fast, django, admin, plain):
    _mk_zone(plain)
    a = _mk_alert(plain, name="triggered")
    a.last_triggered_at = datetime.datetime(
        2026, 6, 1, 12, tzinfo=datetime.timezone.utc
    )
    a.save(update_fields=["last_triggered_at"])
    dj, fp = _both(fast, django, admin, f"/users/{plain.username}/activity")
    _assert_identical(dj, fp)


def test_sensor_units_get_and_patch_identical(fast, django, admin, plain):
    dj, fp = _both(fast, django, admin, f"/users/{plain.username}/sensor-units")
    _assert_identical(dj, fp)
    # PATCH upsert on BOTH surfaces then compare the merged map.
    tok = _token(admin)
    dj = django.patch(
        f"/users/{plain.username}/sensor-units",
        {"temperatureweather": "°F"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.patch(
        f"/users/{plain.username}/sensor-units",
        json={"temperatureweather": "°F"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


# ===========================================================================
# sensor-data explorer
# ===========================================================================
def test_sensor_data_catalog_identical(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/admin/sensor-data/catalog")
    _assert_identical(dj, fp)


def test_sensor_data_list_identical(fast, django, admin, plain):
    from apps.sensors.models import TemperatureWeather

    z = _mk_zone(plain)
    for i, v in enumerate([21.5, 22.0, 23.5]):
        _mk_reading(
            TemperatureWeather,
            plain,
            z,
            v,
            datetime.datetime(2026, 6, 1, 10 + i, tzinfo=datetime.timezone.utc),
        )
    dj, fp = _both(
        fast,
        django,
        admin,
        f"/admin/sensor-data?sensor=temperatureweather&username={plain.username}",
    )
    _assert_identical(dj, fp)
    dj, fp = _both(
        fast,
        django,
        admin,
        "/admin/sensor-data?sensor=temperatureweather&from=2026-06-01T11:00:00%2B00:00",
    )
    _assert_identical(dj, fp)


def test_sensor_data_unknown_404(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/admin/sensor-data?sensor=nope")
    _assert_identical(dj, fp, status=404)


def test_sensor_data_patch_and_delete(fast, django, admin, plain):
    from apps.sensors.models import TemperatureWeather

    z = _mk_zone(plain)
    r_dj = _mk_reading(
        TemperatureWeather,
        plain,
        z,
        20.0,
        datetime.datetime(2026, 6, 1, 10, tzinfo=datetime.timezone.utc),
    )
    tok = _token(admin)
    # patch value (structural: same shape, refetch)
    fp = fast.patch(
        f"/admin/sensor-data/temperatureweather/{r_dj.id}",
        json={"value": 99.0},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert fp.status_code == 200, fp.text
    assert fp.json()["value"] == 99.0
    r_dj.refresh_from_db()
    assert r_dj.value == 99.0
    # delete
    fp = fast.delete(
        f"/admin/sensor-data/temperatureweather/{r_dj.id}",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert fp.status_code == 200 and fp.json() == {"status": "deleted"}
    assert not TemperatureWeather.objects.filter(id=r_dj.id).exists()


def test_sensor_data_range_delete_guard_and_delete(fast, django, admin, plain):
    from apps.sensors.models import TemperatureWeather

    z = _mk_zone(plain)
    # guard: missing zone_id/bounds → 400 byte-parity
    dj, fp = _both(
        fast,
        django,
        admin,
        "/admin/sensor-data/temperatureweather",
        method="delete",
    )
    _assert_identical(dj, fp, status=400)
    # real range delete on fastapp
    for i in range(3):
        _mk_reading(
            TemperatureWeather,
            plain,
            z,
            float(i),
            datetime.datetime(2026, 6, 1, 10 + i, tzinfo=datetime.timezone.utc),
        )
    tok = _token(admin)
    fp = fast.delete(
        f"/admin/sensor-data/temperatureweather?zone_id={z.id}"
        "&from=2026-06-01T00:00:00%2B00:00&to=2026-06-02T00:00:00%2B00:00",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert fp.status_code == 200
    assert fp.json() == {"status": "deleted", "deleted": 3}


# ===========================================================================
# backfill
# ===========================================================================
def test_backfill_status_zone_404(fast, django, admin, plain):
    dj, fp = _both(
        fast,
        django,
        admin,
        f"/admin/users/{plain.username}/zones/99999999/backfill-status",
    )
    _assert_identical(dj, fp, status=404)


def test_backfill_status_counts_match(fast, django, admin, plain):
    from apps.sensors.models import TemperatureWeather

    z = _mk_zone(plain)
    _mk_reading(
        TemperatureWeather,
        plain,
        z,
        20.0,
        datetime.datetime(2026, 6, 1, 10, tzinfo=datetime.timezone.utc),
    )
    dj, fp = _both(
        fast,
        django,
        admin,
        f"/admin/users/{plain.username}/zones/{z.id}/backfill-status",
    )
    assert dj.status_code == fp.status_code == 200
    dj_j, fp_j = dj.json(), fp.json()
    for k in (
        "zone_id",
        "zone_name",
        "series_total",
        "series_with_data",
        "last_data_at",
    ):
        assert dj_j[k] == fp_j[k], k


def test_backfill_no_data_400(fast, django, admin, plain):
    z = _mk_zone(plain)
    dj, fp = _both(
        fast,
        django,
        admin,
        f"/admin/users/{plain.username}/zones/{z.id}/backfill",
        method="post",
        data={"interval_minutes": 60, "dry_run": True},
    )
    _assert_identical(dj, fp, status=400)


def test_backfill_dry_run_rows_match(fast, django, admin, plain):
    from apps.sensors.models import TemperatureWeather

    z = _mk_zone(plain)
    _mk_reading(
        TemperatureWeather,
        plain,
        z,
        20.0,
        datetime.datetime(2026, 6, 1, 10, tzinfo=datetime.timezone.utc),
    )
    body = {
        "start": "2026-06-01T11:00:00+00:00",
        "end": "2026-06-01T15:00:00+00:00",
        "interval_minutes": 60,
        "dry_run": True,
    }
    dj, fp = _both(
        fast,
        django,
        admin,
        f"/admin/users/{plain.username}/zones/{z.id}/backfill",
        method="post",
        data=body,
    )
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    dj_j, fp_j = dj.json(), fp.json()
    assert dj_j["rows_created"] == fp_j["rows_created"]
    assert dj_j["dry_run"] is True and fp_j["dry_run"] is True
    assert set(dj_j) == set(fp_j)


# ===========================================================================
# impersonation
# ===========================================================================
def test_impersonate_user_not_found_404(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/admin/impersonate/ghost", method="post")
    _assert_identical(dj, fp, status=404)


def test_impersonate_mints_readonly_token(fast, django, admin, plain):
    tok = _token(admin)
    fp = fast.post(
        f"/admin/impersonate/{plain.username}",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert fp.status_code == 200, fp.text
    body = fp.json()
    assert set(body) == {"access", "username", "readonly", "expires_in"}
    assert body["username"] == plain.username
    assert body["readonly"] is True
    assert body["expires_in"] == 1800
    claims = jwt.decode(body["access"], dj_settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["token_type"] == "access"
    assert claims["user_id"] == plain.id
    assert claims["readonly"] is True
    assert claims["impersonator"] == admin.id
    assert claims["impersonator_username"] == admin.username
    # ~30 min TTL
    assert 1700 <= claims["exp"] - claims["iat"] <= 1900


# ===========================================================================
# monitoring byte-parity fix: DatabaseScheduler *solar* schedule
# ===========================================================================
def test_monitoring_solar_schedule_byte_parity(fast, django, admin):
    from django_celery_beat.models import PeriodicTask, SolarSchedule

    solar, _ = SolarSchedule.objects.get_or_create(
        event="sunrise", latitude=33.5, longitude=-7.6
    )
    PeriodicTask.objects.create(name="pt-solar", task="agriapi.tasks.x", solar=solar)
    dj, fp = _both(fast, django, admin, "/admin/monitoring/tasks")
    _assert_identical(dj, fp)
    dj, fp = _both(fast, django, admin, "/admin/monitoring/overview")
    _assert_identical(dj, fp)
