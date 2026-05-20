"""
Behaviour tests for the plug-and-play alert module.

Covers:
  - Pure threshold predicates (no DB)
  - evaluate_alert / latest_value_for using mocked sensor rows
  - sensor key registry resolves to live models
  - CRUD endpoints (list, create, retrieve, update, delete)
  - Plug-and-play /alerts/for-graph/ endpoint annotation shape
  - Multi-user isolation

Run:
    SECRET_KEY=k DEBUG=True ALLOWED_HOSTS=* USE_POSTGRES=False \
        EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
        uv run python manage.py test analytics.tests.test_alerts -v 1
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from unittest.mock import patch

from analytics.alerts import (
    EQUALITY_TOLERANCE,
    SENSOR_KEY_REGISTRY,
    assert_keys_resolve,
    dispatch_alerts_for_reading,
    evaluate,
    evaluate_alert,
    get_sensor_model,
    grace_period_seconds_for,
    latest_value_for,
    recent_triggers_for_user,
)
from analytics.models import (
    Alert,
    SoilMoistureMedium,
    TemperatureWeather,
    Zone,
)

User = get_user_model()


def _user(username="alice"):
    u = User.objects.create(
        username=username,
        email=f"{username}@example.com",
        firstname=username.title(),
        lastname="Doe",
        is_active=True,
    )
    u.set_password("pw")
    u.save()
    return u


def _zone(user, name="zone-A"):
    return Zone.objects.create(
        user=user,
        name=name,
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _alert(user, *, zone=None, **kwargs):
    payload = {
        "name": "Heat",
        "type": "Weather Temperature",
        "description": "",
        "condition": ">",
        "condition_nbr": Decimal("30.00"),
        "sensor_key": "temperature_weather",
        "zone": zone,
        "is_active": True,
        "user": user,
    }
    payload.update(kwargs)
    return Alert.objects.create(**payload)


# ---------------------------------------------------------------------------
# 1. Pure threshold predicate
# ---------------------------------------------------------------------------


class EvaluatePureTests(TestCase):
    def test_greater_than(self):
        self.assertTrue(evaluate(">", 30, 31))
        self.assertFalse(evaluate(">", 30, 30))
        self.assertFalse(evaluate(">", 30, 29))

    def test_less_than(self):
        self.assertTrue(evaluate("<", 10, 9.5))
        self.assertFalse(evaluate("<", 10, 10))

    def test_equal_to_uses_tolerance(self):
        self.assertTrue(evaluate("=", 7, 7))
        # within tolerance
        self.assertTrue(evaluate("=", 7, 7 + EQUALITY_TOLERANCE / 2))
        # well outside tolerance
        self.assertFalse(evaluate("=", 7, 7.5))

    def test_none_value_never_triggers(self):
        for cond in (">", "<", "="):
            self.assertFalse(evaluate(cond, 10, None))

    def test_unknown_condition_raises(self):
        with self.assertRaises(ValueError):
            evaluate("?", 10, 5)


# ---------------------------------------------------------------------------
# 2. Sensor-key registry sanity
# ---------------------------------------------------------------------------


class SensorRegistryTests(TestCase):
    def test_every_key_resolves_to_a_real_model(self):
        # No assertion = no exception means all keys map to live models.
        assert_keys_resolve(SENSOR_KEY_REGISTRY.keys())

    def test_get_sensor_model_returns_class(self):
        self.assertIs(get_sensor_model("temperature_weather"), TemperatureWeather)


# ---------------------------------------------------------------------------
# 3. evaluate_alert + latest_value_for against synthetic rows
# ---------------------------------------------------------------------------


class EvaluateAlertTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.zone = _zone(self.user)

    def test_latest_value_returns_none_when_no_rows(self):
        alert = _alert(self.user, zone=self.zone)
        latest = latest_value_for(alert)
        self.assertIsNone(latest.value)
        self.assertIsNone(latest.timestamp)

    def test_latest_value_picks_most_recent(self):
        alert = _alert(self.user, zone=self.zone)
        now = timezone.now()
        TemperatureWeather.objects.create(
            zone=self.zone,
            user=self.user,
            value=20.0,
            timestamp=now - timedelta(hours=2),
        )
        TemperatureWeather.objects.create(
            zone=self.zone,
            user=self.user,
            value=33.0,
            timestamp=now,
        )
        latest = latest_value_for(alert)
        self.assertEqual(latest.value, 33.0)

    def test_evaluate_alert_uses_threshold(self):
        alert = _alert(self.user, zone=self.zone, condition_nbr=Decimal("30"))
        self.assertTrue(evaluate_alert(alert, 31.0))
        self.assertFalse(evaluate_alert(alert, 30.0))
        self.assertFalse(evaluate_alert(alert, None))

    def test_unknown_sensor_key_returns_no_reading(self):
        alert = _alert(self.user, zone=self.zone, sensor_key="not_a_thing")
        self.assertIsNone(latest_value_for(alert).value)


# ---------------------------------------------------------------------------
# 4. recent_triggers_for_user fan-out
# ---------------------------------------------------------------------------


class RecentTriggersTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.other = _user("bob")
        self.zone = _zone(self.user)

    def test_isolates_users(self):
        _alert(self.user, zone=self.zone, name="mine")
        _alert(self.other, zone=_zone(self.other), name="theirs")
        rows = recent_triggers_for_user(self.user)
        self.assertEqual([r["name"] for r in rows], ["mine"])

    def test_filters_by_sensor_key(self):
        _alert(self.user, zone=self.zone, sensor_key="temperature_weather", name="t")
        _alert(self.user, zone=self.zone, sensor_key="soil_moisture_medium", name="s")
        rows = recent_triggers_for_user(self.user, sensor_key="temperature_weather")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sensor_key"], "temperature_weather")

    def test_inactive_excluded(self):
        _alert(self.user, zone=self.zone, name="off", is_active=False)
        self.assertEqual(recent_triggers_for_user(self.user), [])

    def test_annotates_latest_and_threshold(self):
        alert = _alert(
            self.user,
            zone=self.zone,
            condition_nbr=Decimal("30"),
            name="hot",
        )
        TemperatureWeather.objects.create(
            zone=self.zone,
            user=self.user,
            value=33.0,
            timestamp=timezone.now(),
        )
        rows = recent_triggers_for_user(self.user)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], alert.id)
        self.assertEqual(row["threshold"], 30.0)
        self.assertEqual(row["latest_value"], 33.0)
        self.assertTrue(row["is_triggered"])
        # last_triggered_at gets stamped on first observation
        alert.refresh_from_db()
        self.assertIsNotNone(alert.last_triggered_at)

    def test_does_not_trigger_when_under_threshold(self):
        alert = _alert(self.user, zone=self.zone, condition_nbr=Decimal("30"))
        TemperatureWeather.objects.create(
            zone=self.zone,
            user=self.user,
            value=20.0,
            timestamp=timezone.now(),
        )
        rows = recent_triggers_for_user(self.user)
        self.assertFalse(rows[0]["is_triggered"])
        alert.refresh_from_db()
        self.assertIsNone(alert.last_triggered_at)


# ---------------------------------------------------------------------------
# 5. CRUD endpoints
# ---------------------------------------------------------------------------


class AlertCRUDTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.other = _user("bob")
        self.zone = _zone(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _create_payload(self, **extra):
        body = {
            "name": "Dry soil",
            "type": "Humidity",
            "description": "Below 20% soil moisture",
            "condition": "<",
            "condition_nbr": "20",
            "sensor_key": "soil_moisture_medium",
            "zone": self.zone.id,
            "is_active": True,
        }
        body.update(extra)
        return body

    def test_unauthenticated_blocked(self):
        c = APIClient()
        r = c.get("/api/alert/")
        self.assertEqual(r.status_code, 401)

    def test_create_assigns_caller_as_owner(self):
        r = self.client.post("/api/alert/", self._create_payload(), format="json")
        self.assertEqual(r.status_code, 201, r.data)
        alert = Alert.objects.get(pk=r.data["id"])
        self.assertEqual(alert.user_id, self.user.id)
        self.assertEqual(alert.sensor_key, "soil_moisture_medium")

    def test_create_rejects_unknown_sensor_key(self):
        r = self.client.post(
            "/api/alert/", self._create_payload(sensor_key="bogus"), format="json"
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("sensor_key", r.data)

    def test_create_rejects_zone_owned_by_someone_else(self):
        other_zone = _zone(self.other, name="theirs")
        r = self.client.post(
            "/api/alert/", self._create_payload(zone=other_zone.id), format="json"
        )
        self.assertEqual(r.status_code, 400)

    def test_list_only_returns_callers_alerts(self):
        _alert(self.user, zone=self.zone, name="mine")
        _alert(self.other, zone=_zone(self.other, name="b"), name="theirs")
        r = self.client.get("/api/alert/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([row["name"] for row in r.data], ["mine"])

    def test_list_filters(self):
        _alert(self.user, zone=self.zone, sensor_key="temperature_weather", name="t")
        _alert(self.user, zone=self.zone, sensor_key="soil_moisture_medium", name="s")
        r = self.client.get("/api/alert/?sensor_key=temperature_weather")
        self.assertEqual([row["name"] for row in r.data], ["t"])

    def test_retrieve_404_for_other_user(self):
        a = _alert(self.other, zone=_zone(self.other, name="b"))
        r = self.client.get(f"/api/alert/{a.id}/")
        self.assertEqual(r.status_code, 404)

    def test_patch_updates_in_place(self):
        a = _alert(self.user, zone=self.zone)
        r = self.client.patch(
            f"/api/alert/{a.id}/", {"condition_nbr": "42"}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db()
        self.assertEqual(float(a.condition_nbr), 42.0)

    def test_delete_removes_row(self):
        a = _alert(self.user, zone=self.zone)
        r = self.client.delete(f"/api/alert/{a.id}/")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(Alert.objects.filter(pk=a.id).exists())

    def test_delete_404_for_other_user(self):
        a = _alert(self.other, zone=_zone(self.other, name="b"))
        r = self.client.delete(f"/api/alert/{a.id}/")
        self.assertEqual(r.status_code, 404)
        self.assertTrue(Alert.objects.filter(pk=a.id).exists())


# ---------------------------------------------------------------------------
# 6. Plug-and-play /alerts/for-graph/
# ---------------------------------------------------------------------------


class AlertsForGraphTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.zone = _zone(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_returns_threshold_payload(self):
        alert = _alert(
            self.user,
            zone=self.zone,
            condition_nbr=Decimal("30"),
            sensor_key="temperature_weather",
        )
        TemperatureWeather.objects.create(
            zone=self.zone,
            user=self.user,
            value=33.0,
            timestamp=timezone.now(),
        )
        r = self.client.get("/api/alerts/for-graph/?sensor_key=temperature_weather")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["alerts"]), 1)
        row = body["alerts"][0]
        self.assertEqual(row["id"], alert.id)
        self.assertEqual(row["threshold"], 30.0)
        self.assertEqual(row["unit"], "°C")
        self.assertTrue(row["is_triggered"])

    def test_unknown_key_returns_400(self):
        r = self.client.get("/api/alerts/for-graph/?sensor_key=bogus")
        self.assertEqual(r.status_code, 400)

    def test_zone_filter_isolates(self):
        z2 = _zone(self.user, name="z2")
        _alert(self.user, zone=self.zone, name="A")
        _alert(self.user, zone=z2, name="B")
        r = self.client.get(
            f"/api/alerts/for-graph/?sensor_key=temperature_weather&zone_id={z2.id}"
        )
        self.assertEqual([row["name"] for row in r.json()["alerts"]], ["B"])


class SensorKeysEndpointTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_returns_registry(self):
        r = self.client.get("/api/alerts/sensor-keys/")
        self.assertEqual(r.status_code, 200)
        keys = {row["key"] for row in r.json()["keys"]}
        self.assertIn("temperature_weather", keys)
        self.assertIn("soil_moisture_medium", keys)


# ---------------------------------------------------------------------------
# 8. dispatch_alerts_for_reading — grace-period + race semantics
# ---------------------------------------------------------------------------


class DispatchAlertsForReadingTests(TestCase):
    """The push-on-ingest path. Every test patches ``send_alert_email.delay``
    so we can assert *what* would be enqueued without a Celery worker."""

    def setUp(self):
        self.user = _user()
        self.zone = _zone(self.user)
        self.alert = _alert(
            self.user, zone=self.zone, condition_nbr=Decimal("20.00"),
            sensor_key="wind_speed", name="High wind",
        )
        self.now = timezone.now()

    def _dispatch(self, value):
        with patch("agriBack.tasks.send_alert_email.delay") as send:
            count = dispatch_alerts_for_reading(
                sensor_key="wind_speed",
                zone=self.zone,
                user=self.user,
                value=value,
                timestamp=self.now,
            )
            return count, send

    def test_no_dispatch_when_value_below_threshold(self):
        count, send = self._dispatch(value=10.0)
        self.assertEqual(count, 0)
        send.assert_not_called()
        self.alert.refresh_from_db()
        self.assertIsNone(self.alert.last_emailed_at)

    def test_dispatch_when_value_above_threshold(self):
        count, send = self._dispatch(value=25.0)
        self.assertEqual(count, 1)
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["alert_id"], self.alert.pk)
        self.assertEqual(kwargs["value"], 25.0)
        self.alert.refresh_from_db()
        self.assertIsNotNone(self.alert.last_emailed_at)
        self.assertIsNotNone(self.alert.last_triggered_at)

    def test_grace_period_blocks_second_dispatch(self):
        # First reading wins, second (right after) must be silenced.
        _, send1 = self._dispatch(value=25.0)
        send1.assert_called_once()
        _, send2 = self._dispatch(value=30.0)
        send2.assert_not_called()

    def test_dispatch_resumes_after_grace_period(self):
        self._dispatch(value=25.0)
        # Backdate the cursor past the wind_speed grace period (15 min default).
        Alert.objects.filter(pk=self.alert.pk).update(
            last_emailed_at=timezone.now() - timedelta(hours=1)
        )
        _, send = self._dispatch(value=25.0)
        send.assert_called_once()

    def test_inactive_alert_is_silent(self):
        Alert.objects.filter(pk=self.alert.pk).update(is_active=False)
        count, send = self._dispatch(value=25.0)
        self.assertEqual(count, 0)
        send.assert_not_called()

    def test_zone_scoped_alert_ignores_other_zone(self):
        other_zone = _zone(self.user, name="z-other")
        with patch("agriBack.tasks.send_alert_email.delay") as send:
            dispatch_alerts_for_reading(
                sensor_key="wind_speed",
                zone=other_zone,
                user=self.user,
                value=25.0,
                timestamp=self.now,
            )
        send.assert_not_called()

    def test_user_wide_alert_fires_for_any_zone(self):
        Alert.objects.filter(pk=self.alert.pk).update(zone=None)
        other_zone = _zone(self.user, name="z-other")
        with patch("agriBack.tasks.send_alert_email.delay") as send:
            dispatch_alerts_for_reading(
                sensor_key="wind_speed",
                zone=other_zone,
                user=self.user,
                value=25.0,
                timestamp=self.now,
            )
        send.assert_called_once()

    def test_unknown_sensor_key_is_silent(self):
        with patch("agriBack.tasks.send_alert_email.delay") as send:
            n = dispatch_alerts_for_reading(
                sensor_key="not_a_real_sensor",
                zone=self.zone,
                user=self.user,
                value=999.0,
                timestamp=self.now,
            )
        self.assertEqual(n, 0)
        send.assert_not_called()

    def test_value_none_is_silent(self):
        with patch("agriBack.tasks.send_alert_email.delay") as send:
            n = dispatch_alerts_for_reading(
                sensor_key="wind_speed",
                zone=self.zone,
                user=self.user,
                value=None,
                timestamp=self.now,
            )
        self.assertEqual(n, 0)
        send.assert_not_called()


class GracePeriodLookupTests(TestCase):
    def test_known_key_returns_configured_value(self):
        # Default fixture in settings.py: water_flow = 5 min = 300 s
        self.assertEqual(grace_period_seconds_for("water_flow"), 300)

    def test_unknown_key_falls_back_to_default(self):
        from django.test import override_settings

        with override_settings(DEFAULT_ALERT_GRACE_PERIOD=42):
            self.assertEqual(grace_period_seconds_for("totally_unknown_key"), 42)

    def test_override_settings_replaces_value(self):
        from django.test import override_settings

        with override_settings(ALERT_GRACE_PERIODS={"wind_speed": 1}):
            self.assertEqual(grace_period_seconds_for("wind_speed"), 1)


# ---------------------------------------------------------------------------
# 9. WeatherIngestAPIView push integration
# ---------------------------------------------------------------------------


class IngestViewAlertDispatchTests(TestCase):
    """Posting to the ingest endpoint must dispatch alerts for every metric
    whose value crosses an active rule."""

    def setUp(self):
        self.user = _user("router-user")
        self.user.username = "Router02"
        self.user.save()
        self.zone = _zone(self.user)
        self.client = APIClient()

    def test_post_above_threshold_dispatches_alert(self):
        _alert(
            self.user, zone=self.zone, sensor_key="wind_speed",
            condition_nbr=Decimal("20.00"), name="High wind",
        )
        payload = {
            "client": "Router02",
            "wind_speed": 25.0,
            "pressure_weather": 1010.0,
        }
        with patch("agriBack.tasks.send_alert_email.delay") as send:
            r = self.client.post(
                "/api/sensors/weather/ingest/", payload, format="json"
            )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["inserted"], 2)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["value"], 25.0)

    def test_post_below_threshold_does_not_dispatch(self):
        _alert(
            self.user, zone=self.zone, sensor_key="wind_speed",
            condition_nbr=Decimal("20.00"), name="High wind",
        )
        payload = {"client": "Router02", "wind_speed": 5.0}
        with patch("agriBack.tasks.send_alert_email.delay") as send:
            r = self.client.post(
                "/api/sensors/weather/ingest/", payload, format="json"
            )
        self.assertEqual(r.status_code, 201)
        send.assert_not_called()

    def test_post_fires_alerts_per_matching_metric(self):
        _alert(
            self.user, zone=self.zone, sensor_key="wind_speed",
            condition_nbr=Decimal("20.00"), name="High wind",
        )
        _alert(
            self.user, zone=self.zone, sensor_key="temperature_weather",
            condition_nbr=Decimal("35.00"), name="Hot day",
        )
        payload = {
            "client": "Router02",
            "wind_speed": 25.0,
            "temperature_weather": 40.0,
        }
        with patch("agriBack.tasks.send_alert_email.delay") as send:
            r = self.client.post(
                "/api/sensors/weather/ingest/", payload, format="json"
            )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(send.call_count, 2)


# ---------------------------------------------------------------------------
# 10. send_alert_email Celery task — defensive branches
# ---------------------------------------------------------------------------


class SendAlertEmailTaskTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.zone = _zone(self.user)
        self.alert = _alert(
            self.user, zone=self.zone, condition_nbr=Decimal("20.00"),
            sensor_key="wind_speed", name="High wind",
        )

    def _call(self, alert_id=None):
        from agriBack.tasks import send_alert_email

        return send_alert_email.apply(
            kwargs=dict(
                alert_id=alert_id or self.alert.pk,
                value=25.0,
                timestamp_iso=timezone.now().isoformat(),
            )
        ).get()

    def test_skips_when_alert_missing(self):
        result = self._call(alert_id=9_999_999)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["reason"], "alert_missing")

    def test_skips_when_alert_inactive(self):
        Alert.objects.filter(pk=self.alert.pk).update(is_active=False)
        result = self._call()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["reason"], "alert_inactive")

    def test_skips_when_no_recipient(self):
        self.user.email = ""
        self.user.save()
        result = self._call()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["reason"], "no_recipient")

    def test_sends_email_when_everything_ok(self):
        from django.core import mail

        result = self._call()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("High wind", mail.outbox[0].subject)
        self.assertIn(self.user.email, mail.outbox[0].to)
