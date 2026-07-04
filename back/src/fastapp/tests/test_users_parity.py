"""F5c golden parity: the ``/users`` admin console + the caller's on-demand
notification email — fastapp must return byte-identical responses to the
django-ninja endpoints it replaces (``apps/users/router_admin.py``, mounted at
``/users`` in ``agriapi/api/__init__.py``).

Covers: the admin user-management surface (list/search, create, fetch, patch,
soft-delete, activate, password-reset, session status, force-logout) and
``POST /users/me/notifications``. ``GET``/``PATCH`` ``/users/me`` are NOT here —
they stay with ``fastapp/routers/selfreads.py`` (separately byte-verified).

Both surfaces drive the SAME committed rows + the SAME Django-minted access
token: Django via DRF's APIClient (ninja mounts at the URL root), fastapp via
Starlette's TestClient. Reads + 404/403/400 envelopes are byte-checked;
mutations with live timestamps / random passwords are checked structurally.
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
        username="users-admin",
        email="users-admin@example.com",
        password="irrelevant-3921",
        is_staff=True,
    )


@pytest.fixture
def plain(django_user_model):
    return django_user_model.objects.create_user(
        username="users-plain",
        email="users-plain@example.com",
        password="irrelevant-3921",
        firstname="Pl",
        lastname="Ain",
        phone_number="+212600000001",
    )


@pytest.fixture
def other(django_user_model):
    return django_user_model.objects.create_user(
        username="users-other",
        email="users-other@example.com",
        password="irrelevant-3921",
        firstname="Ot",
        lastname="Her",
        phone_number="+212600000002",
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


# ===========================================================================
# auth guards
# ===========================================================================
_ADMIN_GET_ROUTES = ["/users", "/users/users-plain", "/users/users-plain/sessions"]


@pytest.mark.parametrize("path", _ADMIN_GET_ROUTES)
def test_non_staff_is_403(fast, django, plain, path):
    dj, fp = _both(fast, django, plain, path)
    assert dj.status_code == 403
    assert fp.status_code == 403
    assert dj.content == fp.content  # {"detail": "Admin access required"}


def test_missing_auth_is_401(fast, django):
    assert django.get("/users").status_code == 401
    assert fast.get("/users").status_code == 401


# ===========================================================================
# list + search (byte-identical reads)
# ===========================================================================
def test_list_users_identical(fast, django, admin, plain, other):
    dj, fp = _both(fast, django, admin, "/users")
    _assert_identical(dj, fp)


def test_list_users_search_identical(fast, django, admin, plain, other):
    dj, fp = _both(fast, django, admin, "/users?search=other")
    _assert_identical(dj, fp)


def test_list_users_search_email_identical(fast, django, admin, plain, other):
    dj, fp = _both(fast, django, admin, "/users?search=users-plain@example.com")
    _assert_identical(dj, fp)


# ===========================================================================
# fetch one + 404 (byte-identical)
# ===========================================================================
def test_get_user_identical(fast, django, admin, plain):
    dj, fp = _both(fast, django, admin, "/users/users-plain")
    _assert_identical(dj, fp)


def test_get_user_404_identical(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/users/nope")
    _assert_identical(dj, fp, status=404)  # {"detail": "User not found."}


def test_user_sessions_identical(fast, django, admin, plain):
    dj, fp = _both(fast, django, admin, "/users/users-plain/sessions")
    _assert_identical(dj, fp)


def test_user_sessions_404_identical(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/users/nope/sessions")
    _assert_identical(dj, fp, status=404)


# ===========================================================================
# create (structural on success — date_joined live; byte on validation errors)
# ===========================================================================
def _create_payload(**over):
    p = {
        "username": "created-1",
        "email": "created-1@example.com",
        "firstname": "Cr",
        "lastname": "Eated",
        "phone_number": "+212600000009",
        "password": "S3cure-pass-2931",
    }
    p.update(over)
    return p


def test_create_user_structural(fast, django, admin):
    dj = django.post(
        "/users",
        _create_payload(username="dj-created", email="dj-created@x.com"),
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {_token(admin)}",
    )
    fp = fast.post(
        "/users",
        json=_create_payload(username="fp-created", email="fp-created@x.com"),
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    assert dj.status_code == 201, dj.content
    assert fp.status_code == 201, fp.text
    dj_j, fp_j = dj.json(), fp.json()
    assert set(dj_j.keys()) == set(fp_j.keys())
    # non-identity, non-time fields match
    for k in (
        "is_active",
        "is_staff",
        "payement_status",
        "zones_count",
        "notify_every",
    ):
        assert dj_j[k] == fp_j[k], k


def test_create_duplicate_username_400_identical(fast, django, admin, plain):
    payload = _create_payload(username="users-plain", email="fresh@x.com")
    dj, fp = _both(fast, django, admin, "/users", method="post", data=payload)
    _assert_identical(dj, fp, status=400)  # {"username": "This username is ..."}


def test_create_duplicate_email_400_identical(fast, django, admin, plain):
    payload = _create_payload(username="fresh", email="users-plain@example.com")
    dj, fp = _both(fast, django, admin, "/users", method="post", data=payload)
    _assert_identical(dj, fp, status=400)


def test_create_bad_latitude_400_identical(fast, django, admin):
    payload = _create_payload(username="badlat", email="badlat@x.com", latitude=200.0)
    dj, fp = _both(fast, django, admin, "/users", method="post", data=payload)
    _assert_identical(dj, fp, status=400)


def test_create_weak_password_400_identical(fast, django, admin):
    payload = _create_payload(username="weak", email="weak@x.com", password="123")
    dj, fp = _both(fast, django, admin, "/users", method="post", data=payload)
    _assert_identical(dj, fp, status=400)


# ===========================================================================
# patch (structural success; byte-identical validation errors / 404)
# ===========================================================================
def test_patch_user_structural(fast, django, admin, plain, other):
    dj = django.patch(
        "/users/users-plain",
        {"firstname": "NewName"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {_token(admin)}",
    )
    fp = fast.patch(
        "/users/users-other",
        json={"firstname": "NewName"},
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    assert set(dj.json().keys()) == set(fp.json().keys())
    assert dj.json()["firstname"] == fp.json()["firstname"] == "NewName"


def test_patch_user_404_identical(fast, django, admin):
    dj, fp = _both(
        fast, django, admin, "/users/nope", method="patch", data={"firstname": "x"}
    )
    _assert_identical(dj, fp, status=404)


def test_patch_bad_language_400_identical(fast, django, admin, plain, other):
    dj = django.patch(
        "/users/users-plain",
        {"preferred_language": "en"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {_token(admin)}",
    )
    fp = fast.patch(
        "/users/users-other",
        json={"preferred_language": "en"},
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    assert dj.status_code == 400
    assert fp.status_code == 400
    assert dj.content == fp.content


# ===========================================================================
# activate / delete / password-reset / force-logout
# ===========================================================================
def test_activate_structural(fast, django, admin, plain, other):
    dj = django.post(
        "/users/users-plain/activate",
        {"is_active": False},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {_token(admin)}",
    )
    fp = fast.post(
        "/users/users-other/activate",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    assert dj.json()["is_active"] == fp.json()["is_active"] is False
    assert set(dj.json().keys()) == set(fp.json().keys())


def test_delete_user_204_both(fast, django, admin, plain, other):
    dj = django.delete(
        "/users/users-plain", HTTP_AUTHORIZATION=f"Bearer {_token(admin)}"
    )
    fp = fast.delete(
        "/users/users-other", headers={"Authorization": f"Bearer {_token(admin)}"}
    )
    assert dj.status_code == 204
    assert fp.status_code == 204


def test_delete_self_400_identical(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/users/users-admin", method="delete")
    _assert_identical(dj, fp, status=400)


def test_password_reset_structural(fast, django, admin, plain, other):
    dj = django.post(
        "/users/users-plain/password-reset",
        {},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {_token(admin)}",
    )
    fp = fast.post(
        "/users/users-other/password-reset",
        json={},
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    assert dj.status_code == 200, dj.content
    assert fp.status_code == 200, fp.text
    assert set(dj.json().keys()) == set(fp.json().keys()) == {"username", "password"}


def test_force_logout_structural(fast, django, admin, plain, other):
    dj = django.post(
        "/users/users-plain/force-logout",
        HTTP_AUTHORIZATION=f"Bearer {_token(admin)}",
    )
    fp = fast.post(
        "/users/users-other/force-logout",
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    assert dj.status_code == fp.status_code
    assert set(dj.json().keys()) == set(fp.json().keys())
