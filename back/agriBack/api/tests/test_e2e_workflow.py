"""End-to-end workflow test for the django-ninja v2 surface.

Simulates one realistic user journey through every migrated endpoint.
Each PR in the DRF → ninja sweep adds another **chapter** to the
single ``test_workflow`` function below so the whole UX runs in one
go and we can see in one place what the dashboard actually exercises.

How to add a chapter
--------------------

When a new feature/endpoint lands, append a new ``# --- N. <name>``
block to ``test_workflow`` that:

* Touches the new endpoint in the same way the dashboard would.
* Asserts the response shape we promise downstream.
* Uses ``ctx`` (a plain dict) to carry IDs / tokens to later chapters.

Numbered chapter headers make the failure output easy to read::

    AssertionError: chapter 3 (list alerts) expected 200 got 500

Conventions
-----------

* All HTTP requests use ``APIClient`` so JWT auth is exercised via the
  same simplejwt code path as production.
* Side-effect-only endpoints (the email-sending ones) **assert 200/202
  but do not verify outbound mail** — that path goes to a real SMTP in
  dev; the unit test stays hermetic by relying on the locmem backend
  (``DJANGO_ENV=test`` settings).
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

USER = {
    "username": "e2e-user",
    "email": "e2e@example.test",
    "firstname": "Ennio",
    "lastname": "Endpoint",
    "phone_number": "+212600000000",
    "password": "Workflow-Pass-2026!",
}


def _signup(client: APIClient) -> None:
    resp = client.post(
        "/auth/signup",
        data=USER,
        format="json",
    )
    assert resp.status_code in (201, 400), (
        f"signup expected 201/400 got {resp.status_code}: {resp.content[:200]!r}"
    )


def _signin(client: APIClient) -> dict:
    resp = client.post(
        "/auth/sessions",
        data={"username": USER["username"], "password": USER["password"]},
        format="json",
    )
    assert resp.status_code == 200, f"signin expected 200 got {resp.status_code}"
    body = resp.json()
    assert {"refresh", "access", "is_staff"} <= body.keys()
    return body


def _auth_headers(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.mark.django_db
def test_workflow():
    """Walk every migrated endpoint as one user would in a session.

    Each ``# --- N. <name>`` block below is one chapter. New PRs
    append chapters at the end so the workflow grows alongside the
    feature surface.
    """
    client = APIClient()
    ctx: dict[str, object] = {}

    # --- 1. signup -------------------------------------------------------
    _signup(client)

    # --- 2. signin (token + session cookie) ------------------------------
    tokens = _signin(client)
    access = tokens["access"]
    ctx["access"] = access

    # --- 3. /auth/header equivalent — analytics /api/header/ -------------
    resp = client.get("/users/me", **_auth_headers(access))
    assert resp.status_code == 200, (
        f"chapter 3 (/api/header/) expected 200 got {resp.status_code}"
    )
    assert resp.json() == {"username": USER["username"]}

    # --- 4. zones-names-per-user (empty for a brand-new user) ------------
    resp = client.get("/zones", **_auth_headers(access))
    assert resp.status_code == 200, (
        f"chapter 4 (zones-names) expected 200 got {resp.status_code}"
    )
    assert resp.json() == []

    # --- 5. alerts/sensor-keys (registry projection) ---------------------
    resp = client.get("/sensors", **_auth_headers(access))
    assert resp.status_code == 200
    body = resp.json()
    assert "keys" in body and len(body["keys"]) >= 30
    sample = body["keys"][0]
    # /sensors catalog returns {key, unit, label, type}.
    assert {"key", "unit", "label"} <= sample.keys()

    # --- 6. alerts/suggest for a known sensor_key ------------------------
    resp = client.get(
        "/alerts/suggest?sensor_key=temperature_weather",
        **_auth_headers(access),
    )
    assert resp.status_code == 200, (
        f"chapter 6 (alerts/suggest) expected 200 got {resp.status_code}"
    )
    suggestion = resp.json()
    assert suggestion["sensor_key"] == "temperature_weather"
    assert {"condition", "condition_nbr", "label", "unit"} <= suggestion.keys()

    # --- 7. alerts/suggest with unknown key → 400 ------------------------
    resp = client.get(
        "/alerts/suggest?sensor_key=nope", **_auth_headers(access)
    )
    assert resp.status_code == 400

    # --- 8. create + list + patch + delete an alert ----------------------
    create_body = {
        "name": "E2E alert",
        "type": "Humidity",
        "description": "smoke",
        "condition": ">",
        "condition_nbr": 25.0,
        "sensor_key": "temperature_weather",
        "is_active": True,
    }
    resp = client.post(
        "/alerts", data=create_body, format="json", **_auth_headers(access),
    )
    assert resp.status_code == 201, (
        f"chapter 8 (create alert) expected 201 got {resp.status_code}: "
        f"{resp.content[:200]!r}"
    )
    alert = resp.json()
    ctx["alert_id"] = alert["id"]

    resp = client.get("/alerts", **_auth_headers(access))
    assert resp.status_code == 200
    rows = resp.json()
    assert any(a["id"] == alert["id"] for a in rows)

    resp = client.patch(
        f"/alerts/{alert['id']}",
        data={"is_active": False},
        format="json",
        **_auth_headers(access),
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    resp = client.delete(
        f"/alerts/{alert['id']}", **_auth_headers(access),
    )
    assert resp.status_code == 204

    # --- 9. alerts/for-graph (now empty again) ---------------------------
    resp = client.get(
        "/alerts/for-graph?sensor_key=temperature_weather",
        **_auth_headers(access),
    )
    assert resp.status_code == 200
    assert resp.json() == {"alerts": []}

    # --- 10. notifications-and-alerts (empty for a new user) -------------
    resp = client.get("/notifications", **_auth_headers(access))
    assert resp.status_code == 200
    assert "notifications" in resp.json()

    # --- 11. zone-notification-outbound (noop branch) --------------------
    resp = client.post(
        "/notifications/zone-outbound",
        data={"channels": {"email": False}},
        format="json",
        **_auth_headers(access),
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "noop"}

    # --- 12. manager-affirmations create + own-list ----------------------
    from analytics.models import ManagerAffirmation

    first_action = next(iter(dict(ManagerAffirmation.ACTION_CHOICES)))
    resp = client.post(
        "/manager-affirmations",
        data={"action": first_action, "payload": {"e2e": True}},
        format="json",
        **_auth_headers(access),
    )
    assert resp.status_code == 201, (
        f"chapter 12 (create affirmation) expected 201 got {resp.status_code}: "
        f"{resp.content[:200]!r}"
    )
    aff_id = resp.json()["id"]

    resp = client.get(
        "/manager-affirmations?status=pending", **_auth_headers(access),
    )
    assert resp.status_code == 200
    assert any(a["id"] == aff_id for a in resp.json())

    # Bogus action → 400 (validation handled by the router, not pydantic).
    resp = client.post(
        "/manager-affirmations",
        data={"action": "BOGUS"},
        format="json",
        **_auth_headers(access),
    )
    assert resp.status_code == 400

    # --- 13. auto-sensor route — PR 7 ------------------------------------
    # The 34 dynamically-registered /api/sensors/<slug>/ routes share a
    # handler. Exercising one is enough to prove the registration path.
    resp = client.get(
        "/sensors/temperatureweather", **_auth_headers(access),
    )
    assert resp.status_code == 200, (
        f"chapter 13 (/api/sensors/temperatureweather/) "
        f"expected 200 got {resp.status_code}"
    )
    rows = resp.json()
    assert isinstance(rows, list)
    # New user has no readings; the wire shape is still a list.

    # Unknown slug → 404 from django-ninja's URL match miss.
    resp = client.get(
        "/sensors/totally-not-a-sensor", **_auth_headers(access),
    )
    assert resp.status_code == 404

    # --- 14. weather ingest (PR 8) — bridge webhook path ----------------
    # Needs a Zone to write to, so create one tied to the e2e user.
    from analytics.models import TemperatureWeather, Zone
    from apps.users.models import CustomUser

    e2e_user = CustomUser.objects.get(username=USER["username"])
    Zone.objects.create(
        user=e2e_user,
        name="E2E zone",
        space=1000,
        critical_moisture_threshold=18.0,
    )

    # All-None payload short-circuits to 200 with detail=all_metrics_none.
    resp = client.post(
        "/ingest/weather",
        data={"client": USER["username"]},
        format="json",
    )
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 0, "detail": "all_metrics_none"}

    # Real metrics + client → 201, rows persisted, dispatch fired (no
    # alerts configured for this user, so no email enqueued).
    payload = {
        "client": USER["username"],
        "temperature_weather": 22.5,
        "humidity_weather": 55.0,
    }
    resp = client.post(
        "/ingest/weather", data=payload, format="json",
    )
    assert resp.status_code == 201
    assert resp.json() == {"inserted": 2}
    assert TemperatureWeather.objects.filter(user=e2e_user).count() == 1

    # Missing client + metrics present → 400.
    resp = client.post(
        "/ingest/weather",
        data={"temperature_weather": 22.5},
        format="json",
    )
    assert resp.status_code == 400

    # Unknown client → 400.
    resp = client.post(
        "/ingest/weather",
        data={"client": "nobody", "temperature_weather": 22.5},
        format="json",
    )
    assert resp.status_code == 400

    # --- 15. admin tree (PR 10) — non-admin path ------------------------
    # The e2e user is NOT staff: every admin endpoint must 403.
    resp = client.get("/users", **_auth_headers(access))
    assert resp.status_code == 403
    resp = client.get("/admin/overview", **_auth_headers(access))
    assert resp.status_code == 403
    resp = client.get(
        "/users/admin/zones", **_auth_headers(access),
    )
    assert resp.status_code == 403

    # Promote the e2e user to staff and re-exercise the admin tree.
    e2e_user.is_staff = True
    e2e_user.save(update_fields=["is_staff"])

    # New access token to pick up is_staff = True.
    admin_tokens = _signin(client)
    admin_access = admin_tokens["access"]

    # /users — at least the other seeded user shows up.
    resp = client.get("/users", **_auth_headers(admin_access))
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)

    # /admin/overview — KPIs payload.
    resp = client.get("/admin/overview", **_auth_headers(admin_access))
    assert resp.status_code == 200
    over = resp.json()
    for k in ("users_total", "users_active", "staff_total", "zones_total"):
        assert k in over

    # /users/<self>/zones — at least the one created in chapter 14.
    resp = client.get(
        f"/users/{USER['username']}/zones",
        **_auth_headers(admin_access),
    )
    assert resp.status_code == 200
    zones = resp.json()
    assert len(zones) >= 1
    zone_id = zones[0]["id"]

    # GET zone params subset.
    resp = client.get(
        f"/users/{USER['username']}/zones/{zone_id}/params",
        **_auth_headers(admin_access),
    )
    assert resp.status_code == 200
    assert "soil_param_TAW" in resp.json()

    # PATCH the zone — proves the validation path.
    resp = client.patch(
        f"/users/{USER['username']}/zones/{zone_id}",
        data={"critical_moisture_threshold": 20.0},
        format="json",
        **_auth_headers(admin_access),
    )
    assert resp.status_code == 200
    assert resp.json()["critical_moisture_threshold"] == 20.0

    # GET active-graph for the same zone (gets-or-creates).
    resp = client.get(
        f"/users/{USER['username']}/zones/{zone_id}/active-graph",
        **_auth_headers(admin_access),
    )
    assert resp.status_code == 200

    # Per-user activity timeline.
    resp = client.get(
        f"/users/{USER['username']}/activity",
        **_auth_headers(admin_access),
    )
    assert resp.status_code == 200
    assert "events" in resp.json()

    # Per-user sensor-units GET (empty for a fresh user).
    resp = client.get(
        f"/users/{USER['username']}/sensor-units",
        **_auth_headers(admin_access),
    )
    assert resp.status_code == 200
    assert resp.json() == {}

    # User soft-delete via DELETE /users/<u>.
    resp = client.delete(
        f"/users/{USER['username']}",
        **_auth_headers(admin_access),
    )
    # Admin cannot delete their own admin account.
    assert resp.status_code == 400

    # Carry the user/access onwards for future chapters added by later PRs.
    ctx["aff_id"] = aff_id
    ctx["next_chapter"] = 16
    assert ctx["access"] == access  # sentinel — ctx kept for later chapters
