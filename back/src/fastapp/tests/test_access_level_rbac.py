"""RBAC access-level tiers (#444) — the reusable ``require_level`` dependency
and its enforcement across the write / delete / user-management surface.

Two levels:

* unit — ``level_rank`` ordering + unknown-floors-to-monitor, and the
  ``require_level`` dependency's allow-at-or-above / 403-below behaviour. No DB.
* integration — real endpoints over ``TestClient`` against the real Postgres
  test DB (dual-ORM: fastapp reads via SQLAlchemy the rows Django writes), with
  three users seeded at each tier. Proves: monitor is refused a write and a
  delete but allowed a read; editor is allowed a write but refused a delete and
  a user-management call; admin is allowed all; a non-admin cannot change
  anyone's level; a user cannot self-escalate; and ``/users/me`` reports the
  caller's tier.

Regression guard: if ``require_level`` were ever bypassed (e.g. replaced with a
pass-through that returns the user unconditionally), every ``*_refused`` /
``*_403`` assertion below flips to a success status — a monitor could delete
and an editor could reach the user-management console — so this file fails
loudly rather than silently widening authority.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from fastapp.auth import (
    LEVEL_ADMIN,
    LEVEL_EDITOR,
    LEVEL_MONITOR,
    AuthedUser,
    level_rank,
    require_level,
)


def _user(level: str) -> AuthedUser:
    return AuthedUser(
        id=1,
        username="u",
        email="u@example.com",
        is_staff=False,
        is_technician=False,
        preferred_language="fr",
        access_level=level,
    )


# ===========================================================================
# Unit — level_rank
# ===========================================================================
def test_level_rank_orders_the_scale():
    assert level_rank(LEVEL_MONITOR) == 0
    assert level_rank(LEVEL_EDITOR) == 1
    assert level_rank(LEVEL_ADMIN) == 2
    # strictly increasing monitor < editor < admin
    assert (
        level_rank(LEVEL_MONITOR) < level_rank(LEVEL_EDITOR) < level_rank(LEVEL_ADMIN)
    )


def test_level_rank_is_case_and_whitespace_insensitive():
    assert level_rank("  ADMIN ") == level_rank(LEVEL_ADMIN)
    assert level_rank("Editor") == level_rank(LEVEL_EDITOR)


@pytest.mark.parametrize("value", [None, "", "  ", "root", "superuser", "editorr"])
def test_level_rank_unknown_or_missing_floors_to_monitor(value):
    # The safest floor: an unknown / missing tier can never satisfy a gate.
    assert level_rank(value) == level_rank(LEVEL_MONITOR) == 0


# ===========================================================================
# Unit — require_level dependency
# ===========================================================================
def test_require_level_allows_at_the_threshold():
    dep = require_level(LEVEL_EDITOR)
    returned = dep(user=_user(LEVEL_EDITOR))
    assert returned.access_level == LEVEL_EDITOR


def test_require_level_allows_above_the_threshold():
    dep = require_level(LEVEL_EDITOR)
    returned = dep(user=_user(LEVEL_ADMIN))
    assert returned.access_level == LEVEL_ADMIN


def test_require_level_refuses_below_the_threshold():
    dep = require_level(LEVEL_EDITOR)
    with pytest.raises(HTTPException) as exc:
        dep(user=_user(LEVEL_MONITOR))
    assert exc.value.status_code == 403


def test_require_admin_refuses_an_editor():
    dep = require_level(LEVEL_ADMIN)
    with pytest.raises(HTTPException) as exc:
        dep(user=_user(LEVEL_EDITOR))
    assert exc.value.status_code == 403


def test_require_level_unknown_access_level_is_refused():
    # A bogus stored tier floors to monitor, so even an editor gate rejects it.
    dep = require_level(LEVEL_EDITOR)
    with pytest.raises(HTTPException) as exc:
        dep(user=_user("bogus-tier"))
    assert exc.value.status_code == 403


# ===========================================================================
# Integration — real endpoints against the real Postgres test DB
# ===========================================================================
from django.conf import settings as dj_settings  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from rest_framework_simplejwt.tokens import AccessToken  # noqa: E402

from fastapp.main import app  # noqa: E402
from fastapp.settings import get_settings  # noqa: E402

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="dual-ORM: fastapp reads the test DB Django writes (needs Postgres)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


def _mk(django_user_model, set_access_level, name, level):
    user = django_user_model.objects.create_user(
        username=name,
        email=f"{name}@example.com",
        password="irrelevant-3921",
        firstname="F",
        lastname="L",
        phone_number="+21260000000",
    )
    set_access_level(user, level)
    return user


@pytest.fixture
def monitor_user(django_user_model, set_access_level):
    return _mk(django_user_model, set_access_level, "rbac-monitor", LEVEL_MONITOR)


@pytest.fixture
def editor_user(django_user_model, set_access_level):
    return _mk(django_user_model, set_access_level, "rbac-editor", LEVEL_EDITOR)


@pytest.fixture
def admin_user(django_user_model, set_access_level):
    return _mk(django_user_model, set_access_level, "rbac-admin", LEVEL_ADMIN)


def _auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


# --- monitor: read yes, write no, delete no --------------------------------
@_requires_pg
def test_monitor_is_read_only(fast, monitor_user):
    h = _auth(monitor_user)
    # read is open to any authenticated user
    assert fast.get("/sectors", headers=h).status_code == 200
    # create (editor tier) is refused
    assert fast.post("/sectors", json={"name": "Nord"}, headers=h).status_code == 403
    # delete (admin tier) is refused — the tier gate precedes ownership, so a
    # non-existent id still 403s rather than 404s
    assert fast.delete("/sectors/1", headers=h).status_code == 403


# --- editor: write yes, delete no, user-mgmt no ----------------------------
@_requires_pg
def test_editor_can_write_but_not_delete_or_manage_users(
    fast, editor_user, monitor_user
):
    h = _auth(editor_user)
    created = fast.post("/sectors", json={"name": "Sud"}, headers=h)
    assert created.status_code == 201, created.text
    sid = created.json()["id"]

    # delete (admin tier) refused, even on the editor's own record
    assert fast.delete(f"/sectors/{sid}", headers=h).status_code == 403

    # a user-management call (admin tier) refused
    resp = fast.post(
        f"/users/{monitor_user.username}/access-level",
        json={"access_level": "editor"},
        headers=h,
    )
    assert resp.status_code == 403


# --- admin: everything -----------------------------------------------------
@_requires_pg
def test_admin_can_write_delete_and_manage_users(fast, admin_user):
    h = _auth(admin_user)
    created = fast.post("/sectors", json={"name": "Est"}, headers=h)
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert fast.delete(f"/sectors/{sid}", headers=h).status_code == 200
    # user-management console is reachable
    assert fast.get("/users", headers=h).status_code == 200


# --- access-level management guardrails ------------------------------------
@_requires_pg
def test_non_admin_cannot_change_a_users_level(fast, editor_user, monitor_user):
    resp = fast.post(
        f"/users/{monitor_user.username}/access-level",
        json={"access_level": "admin"},
        headers=_auth(editor_user),
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Admin access required"}


@_requires_pg
def test_admin_cannot_change_their_own_level(fast, admin_user):
    resp = fast.post(
        f"/users/{admin_user.username}/access-level",
        json={"access_level": "editor"},
        headers=_auth(admin_user),
    )
    assert resp.status_code == 400
    assert "your own access level" in resp.json()["detail"]


@_requires_pg
def test_set_access_level_rejects_an_unknown_value(fast, admin_user, monitor_user):
    resp = fast.post(
        f"/users/{monitor_user.username}/access-level",
        json={"access_level": "root"},
        headers=_auth(admin_user),
    )
    assert resp.status_code == 400
    assert "access_level" in resp.json()


@_requires_pg
def test_admin_promotes_then_the_new_tier_takes_effect(fast, admin_user, monitor_user):
    # promote the monitor to editor
    resp = fast.post(
        f"/users/{monitor_user.username}/access-level",
        json={"access_level": "editor"},
        headers=_auth(admin_user),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"username": monitor_user.username, "access_level": "editor"}

    # the change is read back on the caller's own next request (tier read from
    # the DB every request, not baked into the token)
    me = fast.get("/users/me", headers=_auth(monitor_user))
    assert me.status_code == 200
    assert me.json()["access_level"] == "editor"
    # and they can now create (editor tier)
    assert (
        fast.post(
            "/sectors", json={"name": "P"}, headers=_auth(monitor_user)
        ).status_code
        == 201
    )


# --- /users/me exposes the tier --------------------------------------------
@_requires_pg
def test_users_me_reports_the_access_level(fast, editor_user):
    me = fast.get("/users/me", headers=_auth(editor_user))
    assert me.status_code == 200
    assert me.json()["access_level"] == "editor"
