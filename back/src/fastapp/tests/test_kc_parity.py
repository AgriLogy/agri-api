"""F3 golden parity: /kc — fastapp crop-calendar CRUD must match the Django
ninja endpoint byte-for-byte.

Dual-ORM: Postgres only + committed rows (fastapp reads via a separate
SQLAlchemy connection).
"""

from __future__ import annotations

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

URL = "/kc"


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
        username="kc-owner", email="kc-owner@example.com", password="irrelevant-3921"
    )


@pytest.fixture
def technician(django_user_model):
    return django_user_model.objects.create_user(
        username="kc-tech",
        email="kc-tech@example.com",
        password="irrelevant-3921",
        is_technician=True,
    )


def _make_zone(user, **overrides):
    from apps.irrigation.models import Zone

    payload = {
        "name": f"zone-{user.username}",
        "space": 1000.0,
        "critical_moisture_threshold": 20.0,
        "pomp_flow_rate": 1.0,
        "elevation_m": 120.0,
    }
    payload.update(overrides)
    return Zone.objects.create(user=user, **payload)


@pytest.fixture
def zone(owner):
    return _make_zone(owner)


def _make_kc_with_periods(user, zone=None):
    from apps.irrigation.models import Kc, KcPeriod, KcPeriodAssignment

    kc = Kc.objects.create(
        user=user, zone=zone, name="Tomato", plant_name="Solanum", number_of_periods=0
    )
    # Insert out of date order to prove the serializer sorts by start_date.
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


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both(fast, django, user, path):
    tok = _token(user)
    dj = django.get(path, HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.get(path, headers={"Authorization": f"Bearer {tok}"})
    return dj, fp


def test_list_is_byte_identical(fast, django, owner, zone):
    _make_kc_with_periods(owner, zone=zone)
    _make_kc_with_periods(owner)
    dj, fp = _both(fast, django, owner, URL)
    assert dj.status_code == fp.status_code == 200
    assert dj.json() == fp.json()
    assert dj.content == fp.content


def test_get_is_byte_identical(fast, django, owner, zone):
    kc = _make_kc_with_periods(owner, zone=zone)
    dj, fp = _both(fast, django, owner, f"{URL}/{kc.id}")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    body = fp.json()
    # sorted by start_date, zone_name resolved
    assert [p["period_name"] for p in body["periods"]] == ["init", "mid"]
    assert body["zone_name"] == zone.name


def test_get_404_is_identical(fast, django, owner):
    dj, fp = _both(fast, django, owner, f"{URL}/99999999")
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


def test_list_technician_403_is_identical(fast, django, technician):
    dj, fp = _both(fast, django, technician, URL)
    assert dj.status_code == fp.status_code == 403
    assert dj.content == fp.content


def _kc_ids(owner):
    from apps.irrigation.models import Kc

    return set(Kc.objects.filter(user=owner).values_list("id", flat=True))


def _serialize_row(kc_id):
    """Read a Kc back through the Django endpoint-equivalent serialize for
    a stable, id-independent comparison of what was persisted."""
    from apps.irrigation.models import Kc

    kc = Kc.objects.get(id=kc_id)
    periods = sorted(
        (
            {
                "period_name": a.period.period_name,
                "start_date": a.period.start_date.isoformat(),
                "end_date": a.period.end_date.isoformat(),
                "kc_value": a.period.kc_value,
            }
            for a in kc.periods.select_related("period").all()
        ),
        key=lambda p: p["start_date"],
    )
    return {
        "name": kc.name,
        "plant_name": kc.plant_name,
        "zone_id": kc.zone_id,
        "number_of_periods": kc.number_of_periods,
        "periods": periods,
    }


def test_create_shape_and_row_match(fast, django, owner, zone):
    body = {
        "name": "Wheat",
        "plant_name": "Triticum",
        "zone_id": zone.id,
        "periods": [
            {
                "period_name": "init",
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
                "kc_value": 0.4,
            },
            {
                "period_name": "dev",
                "start_date": "2026-02-02",
                "end_date": "2026-03-01",
                "kc_value": 0.8,
            },
        ],
    }
    tok = _token(owner)

    seen = _kc_ids(owner)
    dj = django.post(URL, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    assert dj.status_code == 201, dj.content
    id_dj = (_kc_ids(owner) - seen).pop()

    seen = _kc_ids(owner)
    fp = fast.post(URL, json=body, headers={"Authorization": f"Bearer {tok}"})
    assert fp.status_code == 201, fp.text
    id_fp = (_kc_ids(owner) - seen).pop()

    assert set(dj.json()) == set(fp.json())
    assert _serialize_row(id_dj) == _serialize_row(id_fp)
    assert _serialize_row(id_fp)["number_of_periods"] == 2


def test_create_zone_not_owned_404(fast, django, owner, technician):
    foreign = _make_zone(technician)
    body = {"name": "X", "plant_name": "Y", "zone_id": foreign.id, "periods": []}
    tok = _token(owner)
    dj = django.post(URL, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.post(URL, json=body, headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


def test_update_replaces_periods_identically(fast, django, owner, zone):
    kc_dj = _make_kc_with_periods(owner, zone=zone)
    kc_fp = _make_kc_with_periods(owner, zone=zone)
    body = {
        "name": "Renamed",
        "plant_name": "Solanum2",
        "zone_id": zone.id,
        "periods": [
            {
                "period_name": "only",
                "start_date": "2026-05-01",
                "end_date": "2026-06-01",
                "kc_value": 1.0,
            }
        ],
    }
    tok = _token(owner)
    dj = django.put(
        f"{URL}/{kc_dj.id}", body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}"
    )
    fp = fast.put(
        f"{URL}/{kc_fp.id}", json=body, headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 200
    assert _serialize_row(kc_dj.id) == _serialize_row(kc_fp.id)
    assert _serialize_row(kc_fp.id)["number_of_periods"] == 1


def test_delete_is_identical(fast, django, owner):
    from apps.irrigation.models import Kc, KcPeriod

    kc_dj = _make_kc_with_periods(owner)
    kc_fp = _make_kc_with_periods(owner)
    tok = _token(owner)
    dj = django.delete(f"{URL}/{kc_dj.id}", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.delete(f"{URL}/{kc_fp.id}", headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    assert not Kc.objects.filter(id__in=[kc_dj.id, kc_fp.id]).exists()
    # periods were cascaded away on both surfaces
    assert KcPeriod.objects.count() == 0
