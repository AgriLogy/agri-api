"""Tests for the admin device/router registry + device-health alerts.

Registry (django-ninja, JWT bearer, mounted at /devices):
  * GET, POST            /devices
  * PATCH, DELETE        /devices/<pk>
Health: agriapi.tasks.classify_device_health (pure) + scan_device_health (task).
"""

from datetime import timedelta

import pytest
from django.utils.timezone import now

URL = "/devices"


def _payload(username, **overrides):
    p = {
        "device_type": "lora",
        "serial": "A8404152DEAD",
        "name": "Field node 1",
        "username": username,
        "is_active": True,
    }
    p.update(overrides)
    return p


@pytest.mark.django_db
class TestDeviceRegistry:
    def test_anonymous_is_401(self, anon_client):
        assert anon_client.get(URL).status_code == 401

    def test_non_admin_is_403(self, user_bearer, normal_user):
        r = user_bearer.post(URL, _payload(normal_user.username), format="json")
        assert r.status_code == 403

    def test_admin_creates_device(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user, name="Bassin")
        r = admin_bearer.post(
            URL,
            _payload(normal_user.username, zone_id=zone.id),
            format="json",
        )
        assert r.status_code == 201
        body = r.json()
        assert body["device_type"] == "lora"
        assert body["serial"] == "A8404152DEAD"
        assert body["user"] == normal_user.username
        assert body["zone"] == zone.id

    def test_duplicate_serial_rejected(self, admin_bearer, normal_user):
        admin_bearer.post(URL, _payload(normal_user.username), format="json")
        r = admin_bearer.post(URL, _payload(normal_user.username), format="json")
        assert r.status_code == 400

    def test_invalid_type_rejected(self, admin_bearer, normal_user):
        r = admin_bearer.post(
            URL, _payload(normal_user.username, device_type="nope"), format="json"
        )
        assert r.status_code == 400

    def test_zone_not_owned_rejected(
        self, admin_bearer, normal_user, other_user, zone_factory
    ):
        foreign = zone_factory(other_user, name="Other")
        r = admin_bearer.post(
            URL,
            _payload(normal_user.username, zone_id=foreign.id),
            format="json",
        )
        assert r.status_code == 400

    def test_list_filter_by_username(self, admin_bearer, normal_user, other_user):
        admin_bearer.post(URL, _payload(normal_user.username), format="json")
        admin_bearer.post(
            URL, _payload(other_user.username, serial="BBBB2222"), format="json"
        )
        r = admin_bearer.get(URL, {"username": normal_user.username})
        assert r.status_code == 200
        rows = r.json()
        assert {row["user"] for row in rows} == {normal_user.username}

    def test_patch_and_delete(self, admin_bearer, normal_user):
        created = admin_bearer.post(
            URL, _payload(normal_user.username), format="json"
        ).json()
        pk = created["id"]
        r = admin_bearer.patch(
            f"{URL}/{pk}", {"is_active": False, "name": "renamed"}, format="json"
        )
        assert r.status_code == 200
        assert r.json()["is_active"] is False
        assert r.json()["name"] == "renamed"
        assert admin_bearer.delete(f"{URL}/{pk}").status_code == 200
        # second delete → 404
        assert admin_bearer.delete(f"{URL}/{pk}").status_code == 404


@pytest.mark.django_db
class TestDeviceSensors:
    """CRUD over /devices/<id>/sensors — the admin-configurable router→sensor map."""

    def _device(self, admin_bearer, username, **overrides):
        return admin_bearer.post(
            URL, _payload(username, **overrides), format="json"
        ).json()["id"]

    def test_non_admin_is_403(self, admin_bearer, user_bearer, normal_user):
        did = self._device(admin_bearer, normal_user.username)
        r = user_bearer.post(
            f"{URL}/{did}/sensors",
            {"tag_name": "ta", "sensor_key": "temperature_weather"},
            format="json",
        )
        assert r.status_code == 403

    def test_attach_lists_and_serializes(self, admin_bearer, normal_user, zone_factory):
        zone = zone_factory(normal_user, name="Bassin")
        did = self._device(admin_bearer, normal_user.username, zone_id=zone.id)
        r = admin_bearer.post(
            f"{URL}/{did}/sensors",
            {"tag_name": "ta", "sensor_key": "temperature_weather", "zone_id": zone.id},
            format="json",
        )
        assert r.status_code == 201, r.content
        body = r.json()
        assert body["tag_name"] == "ta"
        assert body["sensor_key"] == "temperature_weather"
        assert body["zone"] == zone.id
        listed = admin_bearer.get(f"{URL}/{did}/sensors").json()
        assert [s["tag_name"] for s in listed] == ["ta"]

    def test_unknown_sensor_key_rejected(self, admin_bearer, normal_user):
        did = self._device(admin_bearer, normal_user.username)
        r = admin_bearer.post(
            f"{URL}/{did}/sensors",
            {"tag_name": "ta", "sensor_key": "bogus"},
            format="json",
        )
        assert r.status_code == 400

    def test_duplicate_tag_rejected(self, admin_bearer, normal_user):
        did = self._device(admin_bearer, normal_user.username)
        payload = {"tag_name": "ta", "sensor_key": "temperature_weather"}
        assert (
            admin_bearer.post(
                f"{URL}/{did}/sensors", payload, format="json"
            ).status_code
            == 201
        )
        r = admin_bearer.post(f"{URL}/{did}/sensors", payload, format="json")
        assert r.status_code == 400

    def test_zone_not_owned_rejected(
        self, admin_bearer, normal_user, other_user, zone_factory
    ):
        did = self._device(admin_bearer, normal_user.username)
        foreign = zone_factory(other_user, name="Other")
        r = admin_bearer.post(
            f"{URL}/{did}/sensors",
            {
                "tag_name": "ta",
                "sensor_key": "temperature_weather",
                "zone_id": foreign.id,
            },
            format="json",
        )
        assert r.status_code == 400

    def test_patch_and_delete(self, admin_bearer, normal_user):
        did = self._device(admin_bearer, normal_user.username)
        sid = admin_bearer.post(
            f"{URL}/{did}/sensors",
            {"tag_name": "ta", "sensor_key": "temperature_weather"},
            format="json",
        ).json()["id"]
        r = admin_bearer.patch(
            f"{URL}/{did}/sensors/{sid}",
            {"sensor_key": "humidity_weather", "is_active": False},
            format="json",
        )
        assert r.status_code == 200
        assert r.json()["sensor_key"] == "humidity_weather"
        assert r.json()["is_active"] is False
        assert admin_bearer.delete(f"{URL}/{did}/sensors/{sid}").status_code == 200
        assert admin_bearer.delete(f"{URL}/{did}/sensors/{sid}").status_code == 404

    def test_attach_to_missing_device_404(self, admin_bearer):
        r = admin_bearer.post(
            f"{URL}/999999/sensors",
            {"tag_name": "ta", "sensor_key": "temperature_weather"},
            format="json",
        )
        assert r.status_code == 404


class TestClassifyDeviceHealth:
    """Pure health classifier — no DB."""

    def test_healthy_recent_good_battery(self):
        from agriapi.tasks import classify_device_health

        ref = now()
        assert (
            classify_device_health(ref - timedelta(hours=1), 3.9, reference=ref) == []
        )

    def test_offline_when_never_seen(self):
        from agriapi.tasks import classify_device_health

        assert classify_device_health(None, 3.9, reference=now()) == ["offline"]

    def test_offline_when_stale(self):
        from agriapi.tasks import classify_device_health

        ref = now()
        assert classify_device_health(
            ref - timedelta(hours=48), 3.9, reference=ref
        ) == ["offline"]

    def test_low_battery(self):
        from agriapi.tasks import classify_device_health

        ref = now()
        assert classify_device_health(ref - timedelta(hours=1), 3.0, reference=ref) == [
            "low_battery"
        ]

    def test_both_issues(self):
        from agriapi.tasks import classify_device_health

        ref = now()
        assert set(
            classify_device_health(ref - timedelta(hours=48), 3.0, reference=ref)
        ) == {"offline", "low_battery"}


@pytest.mark.django_db
class TestScanDeviceHealth:
    def _device(self, user, serial="A8404152DEAD"):
        from apps.irrigation.models import Device

        return Device.objects.create(
            user=user, device_type="lora", serial=serial, is_active=True
        )

    def _uplink(self, serial, *, hours_ago, battery_v):
        from apps.lorawan.chirpstack.models import LoraUplink

        return LoraUplink.objects.create(
            dev_eui=serial,
            received_at=now() - timedelta(hours=hours_ago),
            battery_v=battery_v,
        )

    def test_unhealthy_device_notifies_owner_once(self, normal_user, mailoutbox):
        from agriapi.tasks import scan_device_health
        from apps.irrigation.models import Device

        normal_user.email = "owner@example.com"
        normal_user.save(update_fields=["email"])
        dev = self._device(normal_user)
        self._uplink(dev.serial, hours_ago=48, battery_v=3.1)  # offline + low

        res = scan_device_health()
        assert res["notified"] == 1
        assert len(mailoutbox) == 1
        assert "owner@example.com" in mailoutbox[0].to
        dev.refresh_from_db()
        assert dev.last_health_notified is not None

        # Second scan inside the cooldown → no new email (atomic dedup claim).
        res2 = scan_device_health()
        assert res2["notified"] == 0
        assert len(mailoutbox) == 1

    def test_healthy_device_not_notified(self, normal_user, mailoutbox):
        from agriapi.tasks import scan_device_health

        normal_user.email = "owner2@example.com"
        normal_user.save(update_fields=["email"])
        dev = self._device(normal_user, serial="HEALTHY0001")
        self._uplink(dev.serial, hours_ago=1, battery_v=3.9)  # recent + good

        res = scan_device_health()
        assert res["notified"] == 0
        assert res["healthy"] == 1
        assert len(mailoutbox) == 0
