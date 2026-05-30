"""Idempotency regression test for ``compute_et0_vpd_hourly`` (#16).

The task timestamps its rows at the end of the closed hour window, but fires
every few minutes in test mode (hourly in prod, and can be retried). Before
#16 each tick appended a fresh ``(zone, timestamp)`` row, so the same hour
bucket accumulated duplicates (a dev run once produced 1358 Et0Calculated rows
for a handful of real buckets). The task now upserts, so re-running for the
same hour keeps exactly one row per (zone, timestamp).

``compute_et0_for_zone`` (the agri-core fetch-and-compute) is mocked so the
test exercises only the persistence/idempotency logic — no weather data or
dual-ORM Postgres needed.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from agriapi.tasks import compute_et0_vpd_hourly
from analytics.models import Et0Calculated, VPDWeather, Zone

User = get_user_model()


def _user():
    u = User.objects.create(
        username="et0task",
        email="et0task@example.com",
        firstname="E",
        lastname="T",
        is_active=True,
    )
    u.set_password("pw")
    u.save()
    return u


def _zone(user):
    return Zone.objects.create(
        user=user,
        name="z",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


class ComputeEt0VpdHourlyIdempotencyTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.zone = _zone(self.user)
        self.ts = timezone.now().replace(minute=0, second=0, microsecond=0)

    def _run(self, et0, vpd):
        with mock.patch(
            "agriapi.tasks.compute_et0_for_zone",
            return_value=SimpleNamespace(
                timestamp=self.ts, et0_mm_per_h=et0, vpd_kpa=vpd
            ),
        ):
            return compute_et0_vpd_hourly()

    def test_rerunning_same_hour_does_not_duplicate(self):
        self._run(0.5, 1.2)
        self._run(0.5, 1.2)  # second tick in the same hour bucket
        self.assertEqual(
            Et0Calculated.objects.filter(zone=self.zone, timestamp=self.ts).count(), 1
        )
        self.assertEqual(
            VPDWeather.objects.filter(zone=self.zone, timestamp=self.ts).count(), 1
        )

    def test_rerun_refreshes_value_in_place(self):
        self._run(0.5, 1.2)
        self._run(0.9, 2.0)  # corrected reading for the same hour
        et0 = Et0Calculated.objects.get(zone=self.zone, timestamp=self.ts)
        vpd = VPDWeather.objects.get(zone=self.zone, timestamp=self.ts)
        self.assertAlmostEqual(et0.value, 0.9)
        self.assertAlmostEqual(vpd.value, 2.0)

    def test_distinct_hours_keep_separate_rows(self):
        self._run(0.5, 1.2)
        self.ts = self.ts + timedelta(hours=1)
        self._run(0.6, 1.3)
        self.assertEqual(Et0Calculated.objects.filter(zone=self.zone).count(), 2)
        self.assertEqual(VPDWeather.objects.filter(zone=self.zone).count(), 2)
