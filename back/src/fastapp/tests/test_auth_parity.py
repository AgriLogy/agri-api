"""F1 parity tests: fastapp must accept exactly the tokens Django mints —
and reject exactly what the Django authenticators reject.

Real ``rest_framework_simplejwt`` tokens are minted for a real Django user
(pytest-django), then presented to the fastapp TestClient. The user lookup
crosses the ORM boundary: Django writes the row, fastapp reads it back via
the agri-core SQLAlchemy session (AGRI_DB_URL is bound to Django's test DB
by the session fixture in back/conftest.py) — so these are dual-ORM tests:

* they need Postgres (skip on sqlite, like the other dual-ORM suites), and
* they need COMMITTED rows (``django_db(transaction=True)``) because the
  SQLAlchemy engine is a separate connection that can't see an open
  transaction.
"""

from __future__ import annotations

import datetime

import jwt as pyjwt
import pytest
from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from fastapi import Depends
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from fastapp.auth import AuthedUser, get_current_staff_user
from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM auth parity requires Postgres (SQLAlchemy reads the "
    "same test DB Django writes)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    """simplejwt signs with Django's SECRET_KEY; make sure fastapp verifies
    with the identical key even when a local back/.env diverges from the
    test-process env."""
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def client():
    # No context manager: lifespan (engine dispose) stays out of the way of
    # the session-scoped AGRI_DB_URL binding.
    return TestClient(app)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        username="fastapp-parity",
        email="fastapp-parity@example.com",
        password="irrelevant-3921",
        preferred_language="ar",
    )


def _get_whoami(client: TestClient, token: str):
    return client.get("/fast/whoami", headers={"Authorization": f"Bearer {token}"})


def test_django_minted_access_token_is_accepted(client, user):
    resp = _get_whoami(client, str(AccessToken.for_user(user)))
    assert resp.status_code == 200
    assert resp.json() == {
        "id": user.id,
        "username": "fastapp-parity",
        "email": "fastapp-parity@example.com",
        "is_staff": False,
        "is_technician": False,
        "preferred_language": "ar",
    }


def test_refresh_token_is_rejected(client, user):
    # token_type == "refresh" — a refresh token must never authenticate a
    # request (same as simplejwt's AUTH_TOKEN_CLASSES=AccessToken on the
    # Django side).
    resp = _get_whoami(client, str(RefreshToken.for_user(user)))
    assert resp.status_code == 401


def test_tampered_signature_is_rejected(client, user):
    token = str(AccessToken.for_user(user))
    tampered = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    assert _get_whoami(client, tampered).status_code == 401


def test_revoked_session_is_rejected(client, user):
    """The admin force-logout kill switch: iat < sessions_revoked_at ⇒ 401
    (whole-second compare, ported from agriapi.api.auth)."""
    token = str(AccessToken.for_user(user))
    # Strictly after the token's integer-second iat.
    user.sessions_revoked_at = timezone.now() + datetime.timedelta(seconds=2)
    user.save(update_fields=["sessions_revoked_at"])
    assert _get_whoami(client, token).status_code == 401


def test_reissued_token_after_revocation_is_accepted(client, user):
    """The flip side of the whole-second compare: a token minted AT/after the
    revocation instant keeps working (re-login after force-logout)."""
    user.sessions_revoked_at = timezone.now() - datetime.timedelta(seconds=2)
    user.save(update_fields=["sessions_revoked_at"])
    assert _get_whoami(client, str(AccessToken.for_user(user))).status_code == 200


def test_inactive_user_is_rejected(client, user):
    token = str(AccessToken.for_user(user))
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert _get_whoami(client, token).status_code == 401


def test_missing_iat_is_rejected(client, user):
    """simplejwt always stamps iat; a hand-rolled token without one must fail
    (decode requires exp+iat; the revocation check also fails safe on it)."""
    claims = {
        "token_type": "access",
        "user_id": user.id,
        "exp": int(timezone.now().timestamp()) + 300,
        # no "iat"
    }
    token = pyjwt.encode(claims, dj_settings.SECRET_KEY, algorithm="HS256")
    assert _get_whoami(client, token).status_code == 401


def test_unknown_user_is_rejected(client, user):
    claims = {
        "token_type": "access",
        "user_id": user.id + 999_999,
        "exp": int(timezone.now().timestamp()) + 300,
        "iat": int(timezone.now().timestamp()),
    }
    token = pyjwt.encode(claims, dj_settings.SECRET_KEY, algorithm="HS256")
    assert _get_whoami(client, token).status_code == 401


def test_missing_credentials_is_401(client):
    assert client.get("/fast/whoami").status_code == 401


def test_staff_dependency_forbids_non_staff_and_admits_staff(client, user):
    path = "/_test/staff-only"
    if not any(getattr(r, "path", None) == path for r in app.routes):

        def _staff_probe(staff: AuthedUser = Depends(get_current_staff_user)):
            return {"id": staff.id}

        app.add_api_route(path, _staff_probe, methods=["GET"])

    token = str(AccessToken.for_user(user))
    resp = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403  # authenticated but not staff

    User = get_user_model()
    admin = User.objects.create_user(
        username="fastapp-parity-admin",
        email="fastapp-parity-admin@example.com",
        password="irrelevant-3921",
        is_staff=True,
    )
    admin_token = str(AccessToken.for_user(admin))
    resp = client.get(path, headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    assert resp.json() == {"id": admin.id}
