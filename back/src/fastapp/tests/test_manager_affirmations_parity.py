"""F3 golden parity: /manager-affirmations — fastapp must match the Django
ninja workflow endpoint (list / create / approve / reject) byte-for-byte, and
apply the approved payload identically.

Dual-ORM: Postgres only + committed rows.
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

URL = "/manager-affirmations"


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
def admin(django_user_model):
    return django_user_model.objects.create_user(
        username="ma-admin",
        email="ma-admin@example.com",
        password="irrelevant-3921",
        is_staff=True,
    )


@pytest.fixture
def requester(django_user_model):
    return django_user_model.objects.create_user(
        username="ma-user", email="ma-user@example.com", password="irrelevant-3921"
    )


def _make_affirmation(user, action="user_reactivate", payload=None):
    from apps.irrigation.models import ManagerAffirmation

    return ManagerAffirmation.objects.create(
        action=action, payload=payload or {}, requested_by=user
    )


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both(fast, django, user, path):
    tok = _token(user)
    dj = django.get(path, HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.get(path, headers={"Authorization": f"Bearer {tok}"})
    return dj, fp


# --- list ------------------------------------------------------------------


def test_list_own_is_byte_identical(fast, django, requester):
    _make_affirmation(requester, payload={"user_id": 1})
    _make_affirmation(requester, action="zone_params_change", payload={})
    dj, fp = _both(fast, django, requester, URL)
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


def test_list_admin_sees_all_identical(fast, django, admin, requester):
    _make_affirmation(requester, payload={"user_id": 1})
    _make_affirmation(admin, action="zone_params_change")
    dj, fp = _both(fast, django, admin, URL)
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    assert len(fp.json()) == 2


def test_list_status_filter_identical(fast, django, requester):
    _make_affirmation(requester)
    dj, fp = _both(fast, django, requester, f"{URL}?status=approved")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    assert fp.json() == []


# --- create ----------------------------------------------------------------


def _ids(user):
    from apps.irrigation.models import ManagerAffirmation

    return set(
        ManagerAffirmation.objects.filter(requested_by=user).values_list(
            "id", flat=True
        )
    )


def _stable_row(aff_id):
    from apps.irrigation.models import ManagerAffirmation

    a = ManagerAffirmation.objects.get(id=aff_id)
    return {
        "action": a.action,
        "payload": a.payload,
        "status": a.status,
        "requested_by_id": a.requested_by_id,
        "decision_note": a.decision_note,
    }


def test_create_shape_and_row_match(fast, django, requester):
    body = {"action": "user_reactivate", "payload": {"user_id": 5}}
    tok = _token(requester)

    seen = _ids(requester)
    dj = django.post(URL, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    assert dj.status_code == 201, dj.content
    id_dj = (_ids(requester) - seen).pop()

    seen = _ids(requester)
    fp = fast.post(URL, json=body, headers={"Authorization": f"Bearer {tok}"})
    assert fp.status_code == 201, fp.text
    id_fp = (_ids(requester) - seen).pop()

    assert set(dj.json()) == set(fp.json())
    assert _stable_row(id_dj) == _stable_row(id_fp)
    assert _stable_row(id_fp)["status"] == "pending"


def test_create_unknown_action_400_is_identical(fast, django, requester):
    body = {"action": "nope"}
    tok = _token(requester)
    dj = django.post(URL, body, format="json", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.post(URL, json=body, headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


# --- approve / reject ------------------------------------------------------


def _decided_shape(resp):
    """Serialized decision minus the volatile decided_at/updated_at."""
    j = dict(resp.json())
    j.pop("decided_at", None)
    j.pop("updated_at", None)
    j.pop("created_at", None)
    j.pop("id", None)
    return j


def test_approve_user_reactivate_applies_identically(
    fast, django, admin, requester, django_user_model
):
    disabled_dj = django_user_model.objects.create_user(
        username="d-dj", email="d-dj@e.com", password="x", is_active=False
    )
    disabled_fp = django_user_model.objects.create_user(
        username="d-fp", email="d-fp@e.com", password="x", is_active=False
    )
    a_dj = _make_affirmation(requester, payload={"user_id": disabled_dj.id})
    a_fp = _make_affirmation(requester, payload={"user_id": disabled_fp.id})

    tok = _token(admin)
    dj = django.post(
        f"{URL}/{a_dj.id}/approve",
        {"note": "ok"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        f"{URL}/{a_fp.id}/approve",
        json={"note": "ok"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    # everything but the volatile timestamps + id matches (payload differs by
    # construction — each surface reactivates a different disabled user).
    dj_shape, fp_shape = _decided_shape(dj), _decided_shape(fp)
    dj_shape.pop("payload"), fp_shape.pop("payload")
    assert dj_shape == fp_shape
    assert fp.json()["status"] == "approved"
    # the side effect applied on both surfaces
    disabled_dj.refresh_from_db()
    disabled_fp.refresh_from_db()
    assert disabled_dj.is_active is True
    assert disabled_fp.is_active is True


def test_reject_flips_status_identically(fast, django, admin, requester):
    a_dj = _make_affirmation(requester, payload={"user_id": 1})
    a_fp = _make_affirmation(requester, payload={"user_id": 1})
    tok = _token(admin)
    dj = django.post(
        f"{URL}/{a_dj.id}/reject",
        {"note": "no"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        f"{URL}/{a_fp.id}/reject",
        json={"note": "no"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert dj.status_code == fp.status_code == 200
    assert _decided_shape(dj) == _decided_shape(fp)
    assert fp.json()["status"] == "rejected"


def test_approve_non_admin_403_is_identical(fast, django, requester):
    a = _make_affirmation(requester)
    tok = _token(requester)
    dj = django.post(
        f"{URL}/{a.id}/approve",
        {},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        f"{URL}/{a.id}/approve", json={}, headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 403
    assert dj.content == fp.content


def test_approve_not_found_404_is_identical(fast, django, admin):
    tok = _token(admin)
    dj = django.post(
        f"{URL}/99999999/approve",
        {},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        f"{URL}/99999999/approve", json={}, headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


def test_approve_already_decided_400_is_identical(fast, django, admin, requester):
    from apps.irrigation.models import ManagerAffirmation

    a_dj = _make_affirmation(requester, payload={"user_id": 1})
    a_fp = _make_affirmation(requester, payload={"user_id": 1})
    ManagerAffirmation.objects.filter(id__in=[a_dj.id, a_fp.id]).update(
        status="approved"
    )
    tok = _token(admin)
    dj = django.post(
        f"{URL}/{a_dj.id}/approve",
        {},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {tok}",
    )
    fp = fast.post(
        f"{URL}/{a_fp.id}/approve", json={}, headers={"Authorization": f"Bearer {tok}"}
    )
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content
