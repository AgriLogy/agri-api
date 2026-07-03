"""F2b golden parity: /sensors — fastapp must return byte-identical responses
to the django-ninja endpoint across ALL 37 sensor slugs.

The high-traffic chart path. For every slug we seed two readings in the same
clock hour (so the hourly average is exercised) via the Django ORM, then drive
BOTH surfaces — Django ninja (APIClient) and fastapp (TestClient) — over the
same committed rows + the same token, asserting byte-identical bodies for the
aggregated mode, the ``raw=true`` mode, the catalog, PATCH, and the unknown-
slug 404.

Dual-ORM: Postgres only + committed rows (fastapp reads via a separate
SQLAlchemy connection).
"""

from __future__ import annotations

import datetime

import pytest
from django.conf import settings as dj_settings
from django.db.models import CharField, FloatField
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


def _slug_for(model_cls) -> str:
    return model_cls.__name__.lower().replace("sensor", "").replace("_", "-")


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def django() -> APIClient:
    return APIClient()


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="sensor-owner",
        email="sensor-owner@example.com",
        password="irrelevant-3921",
    )


@pytest.fixture
def zone(owner):
    from apps.irrigation.models import Zone

    return Zone.objects.create(
        user=owner,
        name="Parcelle S",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )


def _seed(model_cls, owner, zone, *, at, floats, chars):
    """Create one reading, setting every FloatField + CharField."""
    kwargs = {"user": owner, "zone": zone, "timestamp": at}
    for f in model_cls._meta.get_fields():
        if isinstance(f, FloatField):
            kwargs[f.name] = floats
        elif isinstance(f, CharField):
            kwargs[f.name] = chars
    return model_cls.objects.create(**kwargs)


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def test_catalog_is_identical(fast, django, owner):
    tok = _token(owner)
    dj = django.get("/sensors", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.get("/sensors", headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == 200 and fp.status_code == 200
    assert dj.json() == fp.json()
    assert dj.content == fp.content


def test_all_sensor_slugs_parity(fast, django, owner, zone):
    """One test, every slug: aggregated + raw byte-parity."""
    from apps.sensors.registry import SENSOR_MODELS

    tok = _token(owner)
    hdr_dj = {"HTTP_AUTHORIZATION": f"Bearer {tok}"}
    hdr_fp = {"Authorization": f"Bearer {tok}"}
    # Two readings in the SAME clock hour → aggregation collapses to one row.
    h = datetime.datetime(2026, 6, 1, 9, tzinfo=datetime.timezone.utc)
    mismatches = []

    for model_cls in SENSOR_MODELS:
        slug = _slug_for(model_cls)
        _seed(
            model_cls,
            owner,
            zone,
            at=h + datetime.timedelta(minutes=5),
            floats=10.0,
            chars="#abc",
        )
        _seed(
            model_cls,
            owner,
            zone,
            at=h + datetime.timedelta(minutes=35),
            floats=20.0,
            chars="#abc",
        )

        for suffix in ("", "?raw=true"):
            url = f"/sensors/{slug}{suffix}"
            dj = django.get(url, **hdr_dj)
            fp = fast.get(url, headers=hdr_fp)
            if dj.status_code != fp.status_code or dj.content != fp.content:
                mismatches.append(
                    f"{url}\n  dj[{dj.status_code}]={dj.content!r}\n"
                    f"  fp[{fp.status_code}]={fp.content!r}"
                )

    assert not mismatches, (
        f"{len(mismatches)} sensor response mismatch(es):\n" + "\n".join(mismatches[:6])
    )


def test_empty_sensor_returns_empty_list_on_both(fast, django, owner):
    tok = _token(owner)
    # temperatureweather with no rows → [] on both
    dj = django.get("/sensors/temperatureweather", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.get(
        "/sensors/temperatureweather", headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 200
    assert dj.json() == fp.json() == []
    assert dj.content == fp.content


def test_unknown_slug_404_on_both(fast, django, owner):
    tok = _token(owner)
    dj = django.get("/sensors/does-not-exist", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.get("/sensors/does-not-exist", headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == 404
    assert fp.status_code == 404


def test_patch_parity(fast, django, owner, zone):
    from apps.sensors.models import TemperatureWeather

    tok = _token(owner)
    r1 = _seed(
        TemperatureWeather,
        owner,
        zone,
        at=datetime.datetime(2026, 6, 1, 9, tzinfo=datetime.timezone.utc),
        floats=10.0,
        chars="",
    )
    r2 = _seed(
        TemperatureWeather,
        owner,
        zone,
        at=datetime.datetime(2026, 6, 1, 9, tzinfo=datetime.timezone.utc),
        floats=10.0,
        chars="",
    )

    dj = django.patch(
        "/sensors/temperatureweather",
        {"id": r1.id, "value": 42.0},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.patch(
        "/sensors/temperatureweather",
        json={"id": r2.id, "value": 42.0},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    dj_body, fp_body = dj.json(), fp.json()
    # ids differ (two rows); compare everything else + the updated value.
    dj_body.pop("id"), fp_body.pop("id")
    assert dj_body == fp_body
    assert fp_body["value"] == 42.0


def test_patch_missing_row_404_is_identical(fast, django, owner):
    tok = _token(owner)
    dj = django.patch(
        "/sensors/temperatureweather",
        {"id": 99999999},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.patch(
        "/sensors/temperatureweather",
        json={"id": 99999999},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == 404 and fp.status_code == 404
    assert dj.json() == fp.json() == {"error": "Not found"}
    assert dj.content == fp.content
