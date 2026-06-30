"""Tests for the admin sensor-data backfill router."""

from datetime import timedelta

import pytest
from django.apps import apps
from django.utils import timezone


def _zone_for(user):
    Zone = apps.get_model("analytics", "zone")
    return Zone.objects.create(
        user=user,
        name="Backfill Zone",
        space=100.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _seed_reading(user, zone, days_ago=20):
    WL = apps.get_model("analytics", "waterlevelsensor")
    ts = timezone.now() - timedelta(days=days_ago)
    return WL.objects.create(user=user, zone=zone, value=42.0, timestamp=ts)


def _url(username, zone_id, suffix=""):
    return f"/admin/users/{username}/zones/{zone_id}/backfill{suffix}"


@pytest.mark.django_db
class TestAuth:
    def test_anonymous_is_401(self, anon_client, normal_user):
        z = _zone_for(normal_user)
        assert anon_client.post(_url(normal_user.username, z.id)).status_code == 401

    def test_non_staff_is_403(self, user_bearer, normal_user):
        z = _zone_for(normal_user)
        resp = user_bearer.post(
            _url(normal_user.username, z.id), data={}, format="json"
        )
        assert resp.status_code == 403


@pytest.mark.django_db
class TestBackfill:
    def test_status_reports_gap(self, admin_bearer, normal_user):
        z = _zone_for(normal_user)
        _seed_reading(normal_user, z, days_ago=10)
        resp = admin_bearer.get(_url(normal_user.username, z.id, "-status"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["series_with_data"] >= 1
        assert data["last_data_at"] is not None
        assert data["gap_hours"] is not None and data["gap_hours"] > 0

    def test_backfill_creates_rows_up_to_now(self, admin_bearer, normal_user):
        z = _zone_for(normal_user)
        _seed_reading(normal_user, z, days_ago=10)
        WL = apps.get_model("analytics", "waterlevelsensor")
        before = WL.objects.filter(user=normal_user, zone=z).count()

        resp = admin_bearer.post(
            _url(normal_user.username, z.id),
            data={"interval_minutes": 1440},  # daily -> ~10 rows
            format="json",
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["rows_created"] > 0
        assert "analytics.waterlevelsensor" in body["per_series"]

        after = WL.objects.filter(user=normal_user, zone=z).count()
        assert after > before
        # New readings carried the value forward (jittered within ~5%).
        newest = (
            WL.objects.filter(user=normal_user, zone=z).order_by("-timestamp").first()
        )
        assert 39.0 <= newest.value <= 45.0

    def test_dry_run_creates_nothing(self, admin_bearer, normal_user):
        z = _zone_for(normal_user)
        _seed_reading(normal_user, z, days_ago=5)
        WL = apps.get_model("analytics", "waterlevelsensor")
        before = WL.objects.filter(user=normal_user, zone=z).count()
        resp = admin_bearer.post(
            _url(normal_user.username, z.id),
            data={"interval_minutes": 1440, "dry_run": True},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.json()["rows_created"] > 0  # would-create count
        assert WL.objects.filter(user=normal_user, zone=z).count() == before

    def test_backfill_is_idempotent(self, admin_bearer, normal_user):
        z = _zone_for(normal_user)
        _seed_reading(normal_user, z, days_ago=5)
        first = admin_bearer.post(
            _url(normal_user.username, z.id),
            data={"interval_minutes": 1440},
            format="json",
        ).json()
        assert first["rows_created"] > 0
        # Second run has nothing new to add up to the same "now".
        second = admin_bearer.post(
            _url(normal_user.username, z.id),
            data={"interval_minutes": 1440},
            format="json",
        ).json()
        assert second["rows_created"] == 0

    def test_empty_zone_400(self, admin_bearer, normal_user):
        z = _zone_for(normal_user)  # no readings seeded
        resp = admin_bearer.post(
            _url(normal_user.username, z.id), data={}, format="json"
        )
        assert resp.status_code == 400
