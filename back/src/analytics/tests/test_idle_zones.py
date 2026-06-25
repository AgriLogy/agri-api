"""Tests for the idle-zone liveness checker (agri-api #37)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from agriapi.tasks import flag_idle_zones
from analytics.models import TemperatureWeather
from apps.irrigation.models import Zone

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _user(username="liv"):
    return User.objects.create(
        username=username,
        email=f"{username}@example.com",
        firstname=username.title(),
        lastname="Z",
        is_active=True,
    )


def _zone(user, name="z"):
    return Zone.objects.create(
        user=user,
        name=name,
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _reading(zone, user, *, hours_ago):
    return TemperatureWeather.objects.create(
        zone=zone,
        user=user,
        value=20.0,
        timestamp=timezone.now() - timedelta(hours=hours_ago),
    )


def test_stale_zone_emails_owner_once_then_throttles(mailoutbox):
    user = _user()
    zone = _zone(user, name="went-silent")
    _reading(zone, user, hours_ago=48)  # last data 48h ago, threshold 24h
    res = flag_idle_zones()
    assert res["flagged"] == 1
    assert len(mailoutbox) == 1
    assert user.email in mailoutbox[0].to
    # Second run within the re-flag window is throttled.
    res2 = flag_idle_zones()
    assert res2["flagged"] == 0
    assert len(mailoutbox) == 1


def test_fresh_zone_not_flagged(mailoutbox):
    user = _user("fresh")
    zone = _zone(user, name="fresh")
    _reading(zone, user, hours_ago=1)
    res = flag_idle_zones()
    assert res["flagged"] == 0
    assert mailoutbox == []


def test_never_reported_zone_skipped(mailoutbox):
    user = _user("empty")
    _zone(user, name="empty")  # no readings at all
    res = flag_idle_zones()
    assert res["flagged"] == 0
    assert mailoutbox == []
