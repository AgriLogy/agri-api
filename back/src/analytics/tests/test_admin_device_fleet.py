"""Tests for the admin device-fleet-health endpoint (django-ninja, JWT bearer auth).

  * GET /admin/devices/health — LoRaWAN fleet health, is_staff only.

Requires JWT + is_staff (403 for non-staff). The aggregation is best-effort over
the chirpstack ``lora_uplink`` table, which may be absent in the sqlite test DB —
the endpoint then returns an empty fleet rather than 500.
"""

import pytest

URL = "/admin/devices/health"


@pytest.mark.django_db
class TestAdminDeviceFleetHealth:
    def test_non_staff_is_403(self, user_bearer):
        resp = user_bearer.get(URL)
        assert resp.status_code == 403

    def test_admin_gets_fleet_shape(self, admin_bearer):
        resp = admin_bearer.get(URL)
        assert resp.status_code == 200
        body = resp.json()
        assert "devices" in body and isinstance(body["devices"], list)
        summary = body["summary"]
        assert {"total", "online", "stale", "offline"} <= set(summary)
        # Each status count is non-negative and totals are consistent.
        assert summary["total"] == len(body["devices"])
        assert (
            summary["online"] + summary["stale"] + summary["offline"]
            == summary["total"]
        )
