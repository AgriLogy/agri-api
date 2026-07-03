"""F5b golden parity: /devices + /technicians + /irrigation — fastapp must
match the Django ninja endpoints it replaces.

Reads + error envelopes are asserted byte-for-byte (both surfaces drive the
SAME committed rows via the SAME Django-minted token). Writes get their own
rows per surface, so they're asserted on response shape + the row each surface
persisted. Technician password creation is proven by verifying the stored hash
against Django's ``check_password``.

Dual-ORM: Postgres only + committed rows (fastapp reads/writes over a separate
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
        username="dv-admin",
        email="dv-admin@example.com",
        password="irrelevant-3921",
        is_staff=True,
    )


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="dv-owner",
        email="dv-owner@example.com",
        password="irrelevant-3921",
    )


@pytest.fixture
def other(django_user_model):
    return django_user_model.objects.create_user(
        username="dv-other",
        email="dv-other@example.com",
        password="irrelevant-3921",
    )


@pytest.fixture
def technician(django_user_model):
    return django_user_model.objects.create_user(
        username="dv-tech",
        email="dv-tech@example.com",
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


def _token(user) -> str:
    return str(AccessToken.for_user(user))


def _both(fast, django, user, path, method="get", **kw):
    tok = _token(user)
    fk = {}
    if "data" in kw:
        fk["json"] = kw["data"]
        kw.setdefault("format", "json")  # ninja parses a JSON body
    dj = getattr(django, method)(path, HTTP_AUTHORIZATION=f"Bearer {tok}", **kw)
    fp = getattr(fast, method)(path, headers={"Authorization": f"Bearer {tok}"}, **fk)
    return dj, fp


# ===========================================================================
# /devices
# ===========================================================================
def _make_device(user, **overrides):
    from apps.irrigation.models import Device

    payload = {"device_type": "lora", "serial": f"SN-{user.username}", "name": "n1"}
    payload.update(overrides)
    return Device.objects.create(user=user, **payload)


def test_devices_list_byte_identical(fast, django, admin, owner, zone):
    _make_device(owner, serial="SN-A")
    _make_device(owner, serial="SN-B", zone=zone, is_active=False)
    dj, fp = _both(fast, django, admin, "/devices")
    assert dj.status_code == fp.status_code == 200, fp.text
    assert dj.content == fp.content


def test_devices_list_filter_byte_identical(fast, django, admin, owner, other):
    _make_device(owner, serial="SN-OWN")
    _make_device(other, serial="SN-OTH")
    dj, fp = _both(fast, django, admin, "/devices?username=dv-owner")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    assert len(fp.json()) == 1


def test_devices_list_non_admin_403_identical(fast, django, owner):
    dj, fp = _both(fast, django, owner, "/devices")
    assert dj.status_code == fp.status_code == 403
    assert dj.content == fp.content


def test_devices_create_missing_fields_400_identical(fast, django, admin):
    dj, fp = _both(fast, django, admin, "/devices", method="post", data={})
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_devices_create_bad_type_400_identical(fast, django, admin):
    body = {"device_type": "nope", "serial": "X1", "username": "dv-admin"}
    dj, fp = _both(fast, django, admin, "/devices", method="post", data=body)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_devices_create_owner_not_found_400_identical(fast, django, admin):
    body = {"device_type": "lora", "serial": "X2", "username": "ghost"}
    dj, fp = _both(fast, django, admin, "/devices", method="post", data=body)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_devices_create_persists_row(fast, django, admin, owner, zone):
    from apps.irrigation.models import Device

    body = {
        "device_type": "bivocom",
        "serial": " SNX ",
        "name": " gw ",
        "username": "dv-owner",
        "zone_id": zone.id,
    }
    fp = fast.post(
        "/devices",
        json=body,
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    assert fp.status_code == 201, fp.text
    data = fp.json()
    assert set(data) == {
        "id",
        "device_type",
        "serial",
        "name",
        "user",
        "zone",
        "is_active",
        "created_at",
    }
    assert data["serial"] == "SNX" and data["name"] == "gw"
    assert data["user"] == "dv-owner" and data["zone"] == zone.id
    assert data["is_active"] is True
    row = Device.objects.get(id=data["id"])
    assert row.serial == "SNX" and row.user_id == owner.id and row.zone_id == zone.id


def test_devices_create_duplicate_serial_400_identical(fast, django, admin, owner):
    _make_device(owner, serial="DUP")
    body = {"device_type": "lora", "serial": "DUP", "username": "dv-owner"}
    dj, fp = _both(fast, django, admin, "/devices", method="post", data=body)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_devices_patch_persists(fast, django, admin, owner):
    from apps.irrigation.models import Device

    dev = _make_device(owner, serial="P1", name="old")
    fp = fast.patch(
        f"/devices/{dev.id}",
        json={"name": "new", "is_active": False},
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    assert fp.status_code == 200, fp.text
    assert fp.json()["name"] == "new" and fp.json()["is_active"] is False
    dev.refresh_from_db()
    assert dev.name == "new" and dev.is_active is False


def test_devices_patch_404_identical(fast, django, admin):
    dj, fp = _both(
        fast, django, admin, "/devices/999999", method="patch", data={"name": "x"}
    )
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


def test_devices_delete_and_404_identical(fast, django, admin, owner):
    from apps.irrigation.models import Device

    dev = _make_device(owner, serial="D1")
    fp = fast.delete(
        f"/devices/{dev.id}", headers={"Authorization": f"Bearer {_token(admin)}"}
    )
    assert fp.status_code == 200 and fp.json() == {"status": "deleted"}
    assert not Device.objects.filter(id=dev.id).exists()
    dj, fp2 = _both(fast, django, admin, "/devices/888888", method="delete")
    assert dj.status_code == fp2.status_code == 404
    assert dj.content == fp2.content


# ===========================================================================
# /technicians
# ===========================================================================
def _make_grant(owner, technician, zones_scope=None):
    from apps.irrigation.models import TechnicianGrant, TechnicianZoneGrant

    grant = TechnicianGrant.objects.create(owner=owner, technician=technician)
    for zid, graphs in zones_scope or []:
        TechnicianZoneGrant.objects.create(
            grant=grant, zone_id=zid, allowed_graphs=graphs
        )
    return grant


def test_technicians_list_byte_identical(fast, django, owner, technician, zone):
    _make_grant(owner, technician, [(zone.id, ["water_flow_status"])])
    dj, fp = _both(fast, django, owner, "/technicians")
    assert dj.status_code == fp.status_code == 200, fp.text
    assert dj.content == fp.content


def test_technicians_detail_byte_identical(fast, django, owner, technician, zone):
    _make_grant(owner, technician, [(zone.id, [])])
    dj, fp = _both(fast, django, owner, f"/technicians/{technician.id}")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


def test_technicians_detail_404_identical(fast, django, owner):
    dj, fp = _both(fast, django, owner, "/technicians/777777")
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


def test_technicians_owner_required_403_identical(fast, django, technician):
    dj, fp = _both(fast, django, technician, "/technicians")
    assert dj.status_code == fp.status_code == 403
    assert dj.content == fp.content


def test_technicians_create_persists_and_password_valid(fast, django, owner, zone):
    from django.contrib.auth.hashers import check_password

    from apps.irrigation.models import TechnicianGrant
    from apps.users.models import CustomUser

    body = {
        "username": "tech-new",
        "password": "Zk7mNq93xW",
        "firstname": "Tech",
        "lastname": "One",
        "email": "t1@example.com",
        "scope": [{"zone_id": zone.id, "allowed_graphs": ["water_flow_status"]}],
    }
    fp = fast.post(
        "/technicians",
        json=body,
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )
    assert fp.status_code == 201, fp.text
    data = fp.json()
    assert set(data) == {
        "id",
        "username",
        "firstname",
        "lastname",
        "email",
        "is_active",
        "scope",
    }
    assert data["username"] == "tech-new" and data["is_active"] is True
    assert data["scope"] == [
        {"zone_id": zone.id, "allowed_graphs": ["water_flow_status"]}
    ]
    tech = CustomUser.objects.get(username="tech-new")
    assert tech.is_technician and not tech.is_staff and tech.is_active
    assert tech.payement_status == "actif" and tech.preferred_language == "fr"
    # The stored hash must verify against Django's check_password.
    assert check_password("Zk7mNq93xW", tech.password)
    assert tech.password.startswith("pbkdf2_sha256$600000$")
    assert TechnicianGrant.objects.filter(
        owner=owner, technician=tech, is_active=True
    ).exists()


def test_technicians_create_weak_password_400_identical(fast, django, owner):
    for pw in ("short", "12345678", "password"):
        body = {"username": f"t-{pw}", "password": pw}
        dj, fp = _both(fast, django, owner, "/technicians", method="post", data=body)
        assert dj.status_code == fp.status_code == 400, (pw, fp.text)
        assert dj.content == fp.content, pw


def test_technicians_create_username_taken_400_identical(fast, django, owner):
    body = {"username": "dv-owner", "password": "Zk7mNq93xW"}
    dj, fp = _both(fast, django, owner, "/technicians", method="post", data=body)
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_technicians_scope_unowned_zone_400_identical(
    fast, django, owner, technician, other
):
    _make_grant(owner, technician)
    foreign = _make_zone(other)
    body = {"scope": [{"zone_id": foreign.id, "allowed_graphs": []}]}
    dj, fp = _both(
        fast,
        django,
        owner,
        f"/technicians/{technician.id}/scope",
        method="put",
        data=body,
    )
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_technicians_scope_set_persists(fast, django, owner, technician, zone):
    from apps.irrigation.models import TechnicianZoneGrant

    grant = _make_grant(owner, technician)
    body = {"scope": [{"zone_id": zone.id, "allowed_graphs": ["npk_status"]}]}
    fp = fast.put(
        f"/technicians/{technician.id}/scope",
        json=body,
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )
    assert fp.status_code == 200, fp.text
    assert fp.json()["scope"] == [
        {"zone_id": zone.id, "allowed_graphs": ["npk_status"]}
    ]
    zg = TechnicianZoneGrant.objects.get(grant=grant)
    assert zg.zone_id == zone.id and zg.allowed_graphs == ["npk_status"]


def test_technicians_reset_password_persists(fast, django, owner, technician):
    from django.contrib.auth.hashers import check_password

    from apps.users.models import CustomUser

    _make_grant(owner, technician)
    fp = fast.post(
        f"/technicians/{technician.id}/reset-password",
        json={"password": "Fresh9pWqz"},
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )
    assert fp.status_code == 200
    assert fp.json() == {"status": "reset", "password": "Fresh9pWqz"}
    tech = CustomUser.objects.get(id=technician.id)
    assert check_password("Fresh9pWqz", tech.password)


def test_technicians_revoke_persists(fast, django, owner, technician):
    from apps.irrigation.models import TechnicianGrant
    from apps.users.models import CustomUser

    grant = _make_grant(owner, technician)
    fp = fast.delete(
        f"/technicians/{technician.id}",
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )
    assert fp.status_code == 200 and fp.json() == {"status": "revoked"}
    grant.refresh_from_db()
    assert grant.is_active is False
    assert CustomUser.objects.get(id=technician.id).is_active is False


# ===========================================================================
# /irrigation
# ===========================================================================
def _make_program(user, zone, **overrides):
    from datetime import time

    from apps.irrigation.models import IrrigationProgram

    payload = {
        "name": "morning",
        "start_time": time(6, 30),
        "weekdays": "1,3,5",
        "enabled": True,
        "duration_min": 30,
    }
    payload.update(overrides)
    return IrrigationProgram.objects.create(user=user, zone=zone, **payload)


def _make_command(user, zone, **overrides):
    from apps.irrigation.models import OutputCommand

    payload = {"action": "open", "source": "manual", "status": "simulated"}
    payload.update(overrides)
    return OutputCommand.objects.create(user=user, zone=zone, **payload)


def test_irrigation_config_byte_identical(fast, django, owner):
    dj, fp = _both(fast, django, owner, "/irrigation/config")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content


def test_irrigation_programs_list_byte_identical(fast, django, owner, zone):
    _make_program(owner, zone, name="p1")
    _make_program(owner, zone, name="p2", target_volume_m3=12.5, duration_min=None)
    dj, fp = _both(fast, django, owner, "/irrigation/programs")
    assert dj.status_code == fp.status_code == 200, fp.text
    assert dj.content == fp.content


def test_irrigation_programs_filter_byte_identical(fast, django, owner, zone):
    other_zone = _make_zone(owner, name="z2")
    _make_program(owner, zone, name="pz1")
    _make_program(owner, other_zone, name="pz2")
    dj, fp = _both(fast, django, owner, f"/irrigation/programs?zone_id={zone.id}")
    assert dj.status_code == fp.status_code == 200
    assert dj.content == fp.content
    assert len(fp.json()) == 1


def test_irrigation_program_create_persists(fast, django, owner, zone):
    from apps.irrigation.models import IrrigationProgram

    body = {
        "name": "eve",
        "zone_id": zone.id,
        "start_time": "18:00:00",
        "weekdays": " 2,4 ",
        "enabled": False,
        "target_volume_m3": 7.0,
    }
    fp = fast.post(
        "/irrigation/programs",
        json=body,
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )
    assert fp.status_code == 200, fp.text
    data = fp.json()
    assert data["name"] == "eve" and data["weekdays"] == "2,4"
    assert data["start_time"] == "18:00:00" and data["enabled"] is False
    row = IrrigationProgram.objects.get(id=data["id"])
    assert row.zone_id == zone.id and row.target_volume_m3 == 7.0


def test_irrigation_program_create_bad_zone_404_identical(fast, django, owner):
    body = {"name": "x", "zone_id": 999999, "start_time": "06:00:00"}
    dj, fp = _both(
        fast, django, owner, "/irrigation/programs", method="post", data=body
    )
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content


def test_irrigation_program_technician_blocked_403_identical(
    fast, django, technician, zone
):
    body = {"name": "x", "zone_id": zone.id, "start_time": "06:00:00"}
    dj, fp = _both(
        fast, django, technician, "/irrigation/programs", method="post", data=body
    )
    assert dj.status_code == fp.status_code == 403
    assert dj.content == fp.content


def test_irrigation_program_update_persists(fast, django, owner, zone):
    from apps.irrigation.models import IrrigationProgram

    p = _make_program(owner, zone)
    body = {
        "name": "updated",
        "zone_id": zone.id,
        "start_time": "07:15:00",
        "weekdays": "6",
        "enabled": True,
        "duration_min": 45,
    }
    fp = fast.put(
        f"/irrigation/programs/{p.id}",
        json=body,
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )
    assert fp.status_code == 200, fp.text
    assert fp.json()["name"] == "updated" and fp.json()["start_time"] == "07:15:00"
    p.refresh_from_db()
    assert p.name == "updated" and p.duration_min == 45


def test_irrigation_program_delete_and_404_identical(fast, django, owner, zone):
    from apps.irrigation.models import IrrigationProgram

    p = _make_program(owner, zone)
    fp = fast.delete(
        f"/irrigation/programs/{p.id}",
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )
    assert fp.status_code == 200 and fp.json() == {"deleted": True}
    assert not IrrigationProgram.objects.filter(id=p.id).exists()
    dj, fp2 = _both(fast, django, owner, "/irrigation/programs/888888", method="delete")
    assert dj.status_code == fp2.status_code == 404
    assert dj.content == fp2.content


def test_irrigation_commands_list_byte_identical(fast, django, owner, zone):
    _make_command(owner, zone, detail="Simulation mode — no hardware actuated.")
    _make_command(owner, zone, action="close")
    dj, fp = _both(fast, django, owner, "/irrigation/commands")
    assert dj.status_code == fp.status_code == 200, fp.text
    assert dj.content == fp.content


def test_irrigation_command_send_simulated_persists(fast, django, owner, zone):
    from apps.irrigation.models import OutputCommand

    body = {"zone_id": zone.id, "action": "open"}
    fp = fast.post(
        "/irrigation/commands",
        json=body,
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )
    assert fp.status_code == 200, fp.text
    data = fp.json()
    assert data["status"] == "simulated"
    assert data["detail"] == "Simulation mode — no hardware actuated."
    assert data["dispatched_at"] is not None
    row = OutputCommand.objects.get(id=data["id"])
    assert row.status == "simulated" and row.action == "open"


def test_irrigation_command_bad_action_400_identical(fast, django, owner, zone):
    body = {"zone_id": zone.id, "action": "wiggle"}
    dj, fp = _both(
        fast, django, owner, "/irrigation/commands", method="post", data=body
    )
    assert dj.status_code == fp.status_code == 400
    assert dj.content == fp.content


def test_irrigation_command_bad_device_404_identical(fast, django, owner, zone):
    body = {"zone_id": zone.id, "action": "open", "device_id": 999999}
    dj, fp = _both(
        fast, django, owner, "/irrigation/commands", method="post", data=body
    )
    assert dj.status_code == fp.status_code == 404
    assert dj.content == fp.content
