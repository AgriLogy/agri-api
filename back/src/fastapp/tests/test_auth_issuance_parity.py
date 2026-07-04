"""F9 golden parity: the login / token-issuance surface — fastapp must return
responses equivalent to the Django endpoints it replaces
(``apps/users/router.py`` ninja ``/auth`` router + ``apps/users/urls.py`` DRF
``/auth/token`` views).

Tokens carry a random ``jti`` and time-based ``exp``/``iat``, so they are never
byte-identical to a Django mint — token responses are compared by DECODED
claims (keys, ``user_id``, ``token_type``, lifetime) and cross-mint validity;
error / status envelopes are byte-checked; signup additionally asserts the same
DB rows (user + bootstrap GraphName/SensorColor) are created on both surfaces.

Both surfaces drive the SAME committed rows + the SAME test DB (Django writes,
fastapp reads via the agri-core SQLAlchemy session). Needs Postgres +
``transaction=True`` (the SQLAlchemy engine is a separate connection).
"""

from __future__ import annotations

import jwt as pyjwt
import pytest
from django.conf import settings as dj_settings
from django.core.cache import cache
from fastapi.testclient import TestClient
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from fastapp.main import app
from fastapp.routers import auth_issuance
from fastapp.settings import get_settings
from fastapp.tokens import ACCESS_TOKEN_LIFETIME, REFRESH_TOKEN_LIFETIME

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM parity requires Postgres",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture(autouse=True)
def _reset_lockout():
    """Both surfaces keep a per-process login-attempt counter — clear both so
    lockout state never leaks across tests."""
    cache.clear()
    auth_issuance._lockout_reset()
    yield
    cache.clear()
    auth_issuance._lockout_reset()


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def django() -> APIClient:
    return APIClient()


@pytest.fixture
def user(django_user_model):
    u = django_user_model.objects.create_user(
        username="auth-user",
        email="auth-user@example.com",
        password="S3cure-pass-2931",
        firstname="Au",
        lastname="Th",
        phone_number="+212600000010",
    )
    return u


def _decode(token: str) -> dict:
    return pyjwt.decode(
        token,
        dj_settings.SECRET_KEY,
        algorithms=["HS256"],
        options={"require": ["exp"]},
    )


def _assert_same_token_claims(dj_tok: str, fp_tok: str, token_type: str, lifetime):
    dj_c, fp_c = _decode(dj_tok), _decode(fp_tok)
    assert set(dj_c.keys()) == set(fp_c.keys()), (dj_c.keys(), fp_c.keys())
    assert dj_c["token_type"] == fp_c["token_type"] == token_type
    assert dj_c["user_id"] == fp_c["user_id"]
    for c in (dj_c, fp_c):
        assert c["exp"] - c["iat"] == int(lifetime.total_seconds())


# ===========================================================================
# signup
# ===========================================================================
def _signup_payload(**over):
    p = {
        "username": "signed-up",
        "email": "signed-up@example.com",
        "firstname": "Si",
        "lastname": "Up",
        "phone_number": "+212600000099",
        "password": "S3cure-pass-2931",
    }
    p.update(over)
    return p


def test_signup_success_identical_and_bootstraps_rows(fast, django, django_user_model):
    from apps.irrigation.models import GraphName
    from apps.sensors.models import SensorColor

    dj = django.post(
        "/auth/signup",
        _signup_payload(username="dj-su", email="dj-su@x.com"),
        format="json",
    )
    fp = fast.post(
        "/auth/signup", json=_signup_payload(username="fp-su", email="fp-su@x.com")
    )
    assert dj.status_code == 201, dj.content
    assert fp.status_code == 201, fp.text
    assert dj.content == fp.content  # {"status": "Account created successfully"}

    # both created a user + exactly one GraphName + one SensorColor (bootstrap)
    for uname in ("dj-su", "fp-su"):
        u = django_user_model.objects.get(username=uname)
        assert u.is_active and not u.is_staff and u.payement_status == "actif"
        assert u.preferred_language == "fr" and u.notify_every == 240
        assert u.check_password("S3cure-pass-2931")  # fastapp hash verifies in Django
        assert GraphName.objects.filter(user=u, zone__isnull=True).count() == 1
        assert SensorColor.objects.filter(user=u, zone__isnull=True).count() == 1
        gn = GraphName.objects.get(user=u, zone__isnull=True)
        assert gn.soil_irrigation == "Irrigation du sol"
        sc = SensorColor.objects.get(user=u, zone__isnull=True)
        assert sc.et0_color == "#497D74"


def test_signup_duplicate_email_400_identical(fast, django, user):
    payload = _signup_payload(username="fresh", email="auth-user@example.com")
    dj = django.post("/auth/signup", payload, format="json")
    fp = fast.post("/auth/signup", json=payload)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_signup_duplicate_username_400_identical(fast, django, user):
    payload = _signup_payload(username="auth-user", email="fresh@x.com")
    dj = django.post("/auth/signup", payload, format="json")
    fp = fast.post("/auth/signup", json=payload)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_signup_weak_password_400_identical(fast, django):
    payload = _signup_payload(username="weakpw", email="weakpw@x.com", password="123")
    dj = django.post("/auth/signup", payload, format="json")
    fp = fast.post("/auth/signup", json=payload)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content  # {"password": [...]}


# ===========================================================================
# sessions (sign-in)
# ===========================================================================
def test_signin_success_same_claims(fast, django, user):
    dj = django.post(
        "/auth/sessions",
        {"username": "auth-user", "password": "S3cure-pass-2931"},
        format="json",
    )
    fp = fast.post(
        "/auth/sessions",
        json={"username": "auth-user", "password": "S3cure-pass-2931"},
    )
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    dj_j, fp_j = dj.json(), fp.json()
    assert (
        set(dj_j.keys())
        == set(fp_j.keys())
        == {
            "refresh",
            "access",
            "is_staff",
            "is_technician",
        }
    )
    assert dj_j["is_staff"] == fp_j["is_staff"] is False
    assert dj_j["is_technician"] == fp_j["is_technician"] is False
    _assert_same_token_claims(
        dj_j["access"], fp_j["access"], "access", ACCESS_TOKEN_LIFETIME
    )
    _assert_same_token_claims(
        dj_j["refresh"], fp_j["refresh"], "refresh", REFRESH_TOKEN_LIFETIME
    )


def test_signin_bad_credentials_401_identical(fast, django, user):
    body = {"username": "auth-user", "password": "wrong-password"}
    dj = django.post("/auth/sessions", body, format="json")
    cache.clear()
    auth_issuance._lockout_reset()
    fp = fast.post("/auth/sessions", json=body)
    assert dj.status_code == fp.status_code == 401
    assert dj.content == fp.content  # {"error": "Invalid credentials"}


def test_signin_empty_credentials_400_identical(fast, django):
    body = {"username": "", "password": ""}
    dj = django.post("/auth/sessions", body, format="json")
    fp = fast.post("/auth/sessions", json=body)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_signin_inactive_user_401(fast, django, user, django_user_model):
    django_user_model.objects.filter(pk=user.pk).update(is_active=False)
    body = {"username": "auth-user", "password": "S3cure-pass-2931"}
    dj = django.post("/auth/sessions", body, format="json")
    cache.clear()
    auth_issuance._lockout_reset()
    fp = fast.post("/auth/sessions", json=body)
    assert dj.status_code == fp.status_code == 401
    assert dj.content == fp.content


def test_admin_sessions_omits_is_staff(fast, django, user):
    body = {"username": "auth-user", "password": "S3cure-pass-2931"}
    dj = django.post("/auth/admin-sessions", body, format="json")
    fp = fast.post("/auth/admin-sessions", json=body)
    assert dj.status_code == fp.status_code == 200
    assert "is_staff" not in dj.json()
    assert set(dj.json().keys()) == set(fp.json().keys())


# ===========================================================================
# logout everywhere
# ===========================================================================
def test_logout_everywhere_structural(fast, django, user):
    tok = str(AccessToken.for_user(user))
    dj = django.delete("/auth/sessions", HTTP_AUTHORIZATION=f"Bearer {tok}")
    fp = fast.delete("/auth/sessions", headers={"Authorization": f"Bearer {tok}"})
    assert dj.status_code == fp.status_code == 200
    assert (
        set(dj.json().keys())
        == set(fp.json().keys())
        == {
            "success",
            "sessions_revoked_at",
        }
    )
    assert dj.json()["success"] == fp.json()["success"] is True


# ===========================================================================
# DRF token + refresh
# ===========================================================================
def test_token_obtain_success_same_claims(fast, django, user):
    body = {"username": "auth-user", "password": "S3cure-pass-2931"}
    dj = django.post("/auth/token/", body, format="json")
    fp = fast.post("/auth/token/", json=body)
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    assert set(dj.json().keys()) == set(fp.json().keys()) == {"refresh", "access"}
    _assert_same_token_claims(
        dj.json()["access"], fp.json()["access"], "access", ACCESS_TOKEN_LIFETIME
    )


def test_token_obtain_bad_credentials_401_identical(fast, django, user):
    body = {"username": "auth-user", "password": "nope"}
    dj = django.post("/auth/token/", body, format="json")
    fp = fast.post("/auth/token/", json=body)
    assert dj.status_code == fp.status_code == 401
    assert dj.content == fp.content


def test_token_refresh_success(fast, django, user):
    refresh = str(RefreshToken.for_user(user))
    dj = django.post("/auth/token/refresh/", {"refresh": refresh}, format="json")
    fp = fast.post("/auth/token/refresh/", json={"refresh": refresh})
    assert dj.status_code == fp.status_code == 200, (dj.content, fp.text)
    assert set(dj.json().keys()) == set(fp.json().keys()) == {"access"}
    # both new access tokens are valid + carry the user
    assert _decode(dj.json()["access"])["user_id"] == user.id
    assert _decode(fp.json()["access"])["user_id"] == user.id


def test_token_refresh_invalid_401_identical(fast, django):
    body = {"refresh": "not-a-real-token"}
    dj = django.post("/auth/token/refresh/", body, format="json")
    fp = fast.post("/auth/token/refresh/", json=body)
    assert dj.status_code == fp.status_code == 401
    assert dj.content == fp.content


def test_token_refresh_disabled_account_401_identical(
    fast, django, user, django_user_model
):
    refresh = str(RefreshToken.for_user(user))
    django_user_model.objects.filter(pk=user.pk).update(is_active=False)
    dj = django.post("/auth/token/refresh/", {"refresh": refresh}, format="json")
    fp = fast.post("/auth/token/refresh/", json={"refresh": refresh})
    assert dj.status_code == fp.status_code == 401
    assert dj.content == fp.content  # {"detail": "Account is disabled."}


# ===========================================================================
# cross-mint: a fastapp-minted access token must satisfy Django simplejwt
# ===========================================================================
def test_fastapp_access_token_accepted_by_simplejwt(fast, user):
    fp = fast.post(
        "/auth/sessions",
        json={"username": "auth-user", "password": "S3cure-pass-2931"},
    )
    access = fp.json()["access"]
    parsed = AccessToken(access)  # raises TokenError if invalid → test fails
    assert parsed["user_id"] == user.id
    assert parsed["token_type"] == "access"
