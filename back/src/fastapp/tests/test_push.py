"""Mobile push (agri-api #37): FCM client, token registration, alert push.

* pure — the FCM HTTP v1 client against an ``httpx.MockTransport`` (auth
  exchange, success, dead-token and malformed-payload errors, unconfigured),
  credentials parsing and the notification wording;
* Postgres — ``/devices/push-tokens`` (register / upsert / move / delete /
  list), the ``send_alert_push`` task and alert dispatch enqueueing it only
  for users with a registered device. ``notification_push_token`` has no
  Django model; conftest creates it from the agri.db definition.
"""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings as dj_settings
from django.db import connection
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp import celery as fastapp_celery
from fastapp import push, tasks_comms
from fastapp.main import app
from fastapp.settings import get_settings

URL = "/devices/push-tokens"


# ---------------------------------------------------------------------------
# FCM client (pure)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def service_account() -> dict:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "project_id": "agrogo-test",
        "private_key_id": "kid-1",
        "private_key": pem,
        "client_email": "push@agrogo-test.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
        "_public": key.public_key(),
    }


@pytest.fixture
def configured(monkeypatch, service_account):
    creds = {k: v for k, v in service_account.items() if not k.startswith("_")}
    monkeypatch.setattr(get_settings(), "fcm_credentials", json.dumps(creds))
    push.reset_token_cache()
    yield service_account
    push.reset_token_cache()


def _transport(send_status=200, send_body=None, calls=None):
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "at-1", "expires_in": 3600}
            )
        return httpx.Response(
            send_status, json=send_body or {"name": "projects/x/messages/1"}
        )

    return httpx.MockTransport(handler), calls


def test_not_configured_sends_nothing(monkeypatch):
    monkeypatch.setattr(get_settings(), "fcm_credentials", "")
    assert push.is_configured() is False
    assert push.send("tok", title="t", body="b") == push.PushResult(
        ok=False, error="not_configured"
    )


def test_credentials_from_file_and_invalid(tmp_path, service_account):
    creds = {k: v for k, v in service_account.items() if not k.startswith("_")}
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(creds))
    assert push.load_service_account(str(path))["project_id"] == "agrogo-test"
    assert push.load_service_account("{not json") is None
    assert push.load_service_account(json.dumps({"project_id": "x"})) is None
    assert push.load_service_account(str(tmp_path / "missing.json")) is None


def test_send_exchanges_a_signed_jwt_then_posts_the_message(configured):
    transport, calls = _transport()
    with httpx.Client(transport=transport) as client:
        result = push.send(
            "device-token",
            title="Alerte",
            body="Corps",
            data={"zone_id": 3},
            client=client,
        )
    assert result == push.PushResult(ok=True)

    auth, send = calls
    assertion = dict(httpx.QueryParams(auth.content.decode()))["assertion"]
    claims = jwt.decode(
        assertion,
        configured["_public"],
        algorithms=["RS256"],
        audience="https://oauth2.googleapis.com/token",
    )
    assert claims["iss"] == configured["client_email"]
    assert claims["scope"] == push.SCOPE

    assert send.url.path == "/v1/projects/agrogo-test/messages:send"
    assert send.headers["Authorization"] == "Bearer at-1"
    msg = json.loads(send.content)["message"]
    assert msg["token"] == "device-token"
    assert msg["notification"] == {"title": "Alerte", "body": "Corps"}
    assert msg["data"] == {"zone_id": "3"}  # FCM data values must be strings


def test_access_token_is_cached_between_sends(configured):
    transport, calls = _transport()
    with httpx.Client(transport=transport) as client:
        push.send("a", title="t", body="b", client=client)
        push.send("b", title="t", body="b", client=client)
    hosts = [c.url.host for c in calls]
    assert hosts.count("oauth2.googleapis.com") == 1
    assert hosts.count("fcm.googleapis.com") == 2


def _fcm_error(status: str, code: str | None) -> dict:
    details = [{"errorCode": code}] if code else []
    return {"error": {"status": status, "details": details}}


@pytest.mark.parametrize(
    ("http", "body", "dead"),
    [
        (404, _fcm_error("NOT_FOUND", "UNREGISTERED"), True),
        (403, _fcm_error("PERMISSION_DENIED", "SENDER_ID_MISMATCH"), True),
        # A malformed payload also comes back INVALID_ARGUMENT: never treat
        # it as a dead token, or one bad message would wipe every device.
        (400, _fcm_error("INVALID_ARGUMENT", "INVALID_ARGUMENT"), False),
        (503, _fcm_error("UNAVAILABLE", None), False),
    ],
)
def test_send_error_classification(configured, http, body, dead):
    transport, _ = _transport(send_status=http, send_body=body)
    with httpx.Client(transport=transport) as client:
        result = push.send("tok", title="t", body="b", client=client)
    assert result.ok is False
    assert result.unregistered is dead
    assert result.error


def test_send_never_raises_on_network_error(configured):
    def boom(request):
        raise httpx.ConnectError("down")

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        result = push.send("tok", title="t", body="b", client=client)
    assert result == push.PushResult(ok=False, error="ConnectError")


def test_push_wording_single_and_digest():
    a = SimpleNamespace(id=1, name="Chaleur", condition=">", condition_nbr=30.0)
    b = SimpleNamespace(id=2, name="Vent", condition=">", condition_nbr=40.0)
    labels = {1: ("Température", "°C", "Parcelle Nord"), 2: ("Vent", "km/h", "Nord")}

    title, body = tasks_comms.push_alert_content([a], value=35.5, labels=labels)
    assert title == "Alerte — Chaleur"
    assert body == "Température : 35.5 °C (seuil > 30) — Parcelle Nord"

    title, body = tasks_comms.push_alert_content([a, b], value=35.5, labels=labels)
    assert title == "Alertes déclenchées (2)"
    assert body == "« Chaleur », « Vent »"


# ---------------------------------------------------------------------------
# Postgres: endpoints, task, dispatch
# ---------------------------------------------------------------------------

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="notification_push_token is created on Postgres only",
)
pg = [_requires_pg, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def _db_env(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)
    yield
    with connection.cursor() as cursor:  # no Django model → never flushed
        cursor.execute("DELETE FROM notification_push_token")


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


def _user(django_user_model, name):
    return django_user_model.objects.create_user(
        username=name, email=f"{name}@example.com", password="irrelevant-3921"
    )


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


def _tokens() -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT user_id, token, platform, app_version FROM notification_push_token "
            "ORDER BY id"
        )
        return cursor.fetchall()


@pytest.mark.usefixtures("_db_env")
class TestRegistration:
    pytestmark = pg

    def test_register_then_refresh(self, fast, django_user_model):
        u = _user(django_user_model, "push-a")
        r = fast.post(
            URL, json={"token": "tok-1", "platform": "android"}, headers=_auth(u)
        )
        assert r.status_code == 201, r.text
        assert r.json()["created"] is True

        r = fast.post(
            URL,
            json={"token": "tok-1", "platform": "android", "app_version": "1.2.0"},
            headers=_auth(u),
        )
        assert r.status_code == 200
        assert r.json()["created"] is False
        assert _tokens() == [(u.id, "tok-1", "android", "1.2.0")]

    def test_token_moves_to_the_last_signed_in_user(self, fast, django_user_model):
        a = _user(django_user_model, "push-a")
        b = _user(django_user_model, "push-b")
        fast.post(URL, json={"token": "shared", "platform": "ios"}, headers=_auth(a))
        fast.post(URL, json={"token": "shared", "platform": "ios"}, headers=_auth(b))
        assert _tokens() == [(b.id, "shared", "ios", "")]

    def test_validation(self, fast, django_user_model):
        u = _user(django_user_model, "push-a")
        bad_platform = fast.post(
            URL, json={"token": "t", "platform": "windows"}, headers=_auth(u)
        )
        assert bad_platform.status_code == 400
        assert (
            fast.post(
                URL, json={"token": "", "platform": "ios"}, headers=_auth(u)
            ).status_code
            == 422
        )
        assert fast.post(URL, json={"token": "t", "platform": "ios"}).status_code in (
            401,
            403,
        )
        assert _tokens() == []

    def test_delete_only_removes_the_callers_token(self, fast, django_user_model):
        a = _user(django_user_model, "push-a")
        b = _user(django_user_model, "push-b")
        fast.post(URL, json={"token": "a-tok", "platform": "android"}, headers=_auth(a))
        r = fast.request("DELETE", URL, json={"token": "a-tok"}, headers=_auth(b))
        assert r.status_code == 204
        assert len(_tokens()) == 1  # b can't remove a's device
        r = fast.request("DELETE", URL, json={"token": "a-tok"}, headers=_auth(a))
        assert r.status_code == 204
        assert _tokens() == []

    def test_list_shows_only_token_tails(self, fast, django_user_model, monkeypatch):
        monkeypatch.setattr(get_settings(), "fcm_credentials", "")
        u = _user(django_user_model, "push-a")
        fast.post(
            URL,
            json={"token": "secret-token-12345678", "platform": "ios"},
            headers=_auth(u),
        )
        body = fast.get(URL, headers=_auth(u)).json()
        assert body["push_configured"] is False
        (device,) = body["devices"]
        assert device["token_tail"] == "…12345678"
        assert "secret" not in json.dumps(body)

    def test_push_route_does_not_collide_with_admin_device_routes(
        self, fast, django_user_model
    ):
        # /devices/{pk} is admin-only; a plain user must still reach push-tokens.
        u = _user(django_user_model, "push-a")
        assert fast.get(URL, headers=_auth(u)).status_code == 200


def _alert(user, **over):
    from apps.alerts.models import Alert

    payload = {
        "name": "Chaleur",
        "type": "Weather Temperature",
        "description": "",
        "condition": ">",
        "condition_nbr": 30,
        "sensor_key": "temperature_weather",
        "is_active": True,
        "notify_email": False,
    }
    payload.update(over)
    return Alert.objects.create(user=user, **payload)


def _register(user_id, token, platform="android"):
    now = datetime.datetime.now(datetime.timezone.utc)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO notification_push_token "
            "(user_id, token, platform, app_version, created_at, last_seen_at) "
            "VALUES (%s, %s, %s, '', %s, %s)",
            [user_id, token, platform, now, now],
        )


@pytest.mark.usefixtures("_db_env", "configured")
class TestSendAlertPush:
    pytestmark = pg

    def test_sends_to_every_device_and_drops_dead_tokens(
        self, django_user_model, monkeypatch
    ):
        u = _user(django_user_model, "push-a")
        alert = _alert(u)
        _register(u.id, "live-token")
        _register(u.id, "dead-token", "ios")
        sent = []

        def fake_send(token, *, title, body, data):
            sent.append((token, title, data["alert_ids"]))
            if token == "dead-token":
                return push.PushResult(
                    ok=False, unregistered=True, error="UNREGISTERED"
                )
            return push.PushResult(ok=True)

        monkeypatch.setattr(push, "send", fake_send)
        result = tasks_comms.send_alert_push(
            alert_ids=[alert.id], value=35.0, timestamp_iso="2026-11-12T10:00:00+00:00"
        )
        assert result == {"sent": 1, "failed": 1, "removed": 1}
        assert {t for t, _, _ in sent} == {"live-token", "dead-token"}
        assert all(title == "Alerte — Chaleur" for _, title, _ in sent)
        assert [row[1] for row in _tokens()] == ["live-token"]

        from apps.irrigation.models import NotificationDeliveryLog

        logged = set(
            NotificationDeliveryLog.objects.filter(channel="push").values_list(
                "status", flat=True
            )
        )
        assert logged == {"sent", "failed"}
        NotificationDeliveryLog.objects.all().delete()

    def test_skips_without_devices_or_config(self, django_user_model, monkeypatch):
        u = _user(django_user_model, "push-a")
        alert = _alert(u)
        args = {"alert_ids": [alert.id], "value": 35.0, "timestamp_iso": "x"}
        assert tasks_comms.send_alert_push(**args)["reason"] == "no_devices"
        monkeypatch.setattr(get_settings(), "fcm_credentials", "")
        _register(u.id, "tok")
        assert tasks_comms.send_alert_push(**args)["reason"] == "push_not_configured"

    def test_inactive_owner_gets_no_push(self, django_user_model):
        u = _user(django_user_model, "push-a")
        alert = _alert(u)
        _register(u.id, "tok")
        u.is_active = False
        u.save()
        result = tasks_comms.send_alert_push(
            alert_ids=[alert.id], value=35.0, timestamp_iso="x"
        )
        assert result["reason"] == "owner_inactive"


@pytest.mark.usefixtures("_db_env")
class TestDeactivationDropsTokens:
    pytestmark = pg

    def test_admin_soft_delete_removes_push_tokens(
        self, fast, django_user_model, set_access_level
    ):
        admin = _user(django_user_model, "push-admin")
        set_access_level(admin, "admin")
        farmer = _user(django_user_model, "push-farmer")
        _register(farmer.id, "farmer-tok")
        _register(admin.id, "admin-tok")
        r = fast.delete(f"/users/{farmer.username}", headers=_auth(admin))
        assert r.status_code == 204, r.text
        assert [row[1] for row in _tokens()] == ["admin-tok"]


@pytest.mark.usefixtures("_db_env")
class TestDispatchEnqueuesPush:
    pytestmark = pg

    def _dispatch(self, user, monkeypatch):
        from agri.core.database import session_scope

        from fastapp.ingest import dispatch_alerts_for_reading

        import fastapp.ingest as ingest_mod

        enqueued: list[tuple[str, dict]] = []
        self.channels: list[list[str]] = []
        monkeypatch.setattr(
            fastapp_celery, "send_task", lambda name, **kw: enqueued.append((name, kw))
        )
        monkeypatch.setattr(
            ingest_mod,
            "record_alert_event",
            lambda session, **kw: self.channels.append(kw["channels"]),
        )
        with session_scope(commit=True) as session:
            dispatch_alerts_for_reading(
                session,
                sensor_key="temperature_weather",
                zone_id=None,
                user_id=user.id,
                value=35.0,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        return enqueued

    def test_one_push_for_all_alerts_when_the_user_has_a_device(
        self, django_user_model, monkeypatch
    ):
        u = _user(django_user_model, "push-a")
        a1 = _alert(u, name="Chaleur")
        a2 = _alert(u, name="Canicule", condition_nbr=33)
        _register(u.id, "tok")

        pushes = [
            kw
            for name, kw in self._dispatch(u, monkeypatch)
            if name.endswith("send_alert_push")
        ]
        assert len(pushes) == 1
        assert pushes[0]["alert_ids"] == [a1.id, a2.id]

        assert self.channels == [["push"], ["push"]]

    def test_no_push_without_a_registered_device(self, django_user_model, monkeypatch):
        u = _user(django_user_model, "push-b")
        _alert(u)
        names = [name for name, _ in self._dispatch(u, monkeypatch)]
        assert not any(n.endswith("send_alert_push") for n in names)
