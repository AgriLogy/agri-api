"""Ownership PROJECTION must agree with ownership FILTERING (issue #423).

Readings are device-keyed: a row belongs to whoever owns its
``analytics_device``. The filters already resolved ownership through that join
(``_owner_user`` / ``_owner_zone``), but the responses projected the RAW
``user_id`` / ``zone_id`` columns — the stale commissioning snapshot. The two
halves disagreed, so:

* an owner got ZERO rows for data they own (the admin explorer's client filter),
* rows came back labelled with a different client than the one filtered on,
* a range-delete hit a different set of rows than the equivalent list,
* a farmer 404'd patching a reading from his own transferred device.

Every test below transfers a device (one-row ``analytics_device`` UPDATE,
readings untouched) and then asserts BOTH directions: the new owner sees the
rows, and nothing is ever labelled with the previous owner.

Postgres-only (dual-ORM committed rows), mirroring ``test_device_read_resolution``.
"""

from __future__ import annotations

import datetime

import pytest
from django.conf import settings as dj_settings
from fastapi.testclient import TestClient
from rest_framework_simplejwt.tokens import AccessToken

from fastapp.main import app
from fastapp.settings import get_settings

_requires_pg = pytest.mark.skipif(
    not dj_settings.DATABASES["default"]["ENGINE"].endswith("postgresql"),
    reason="device ownership projection requires Postgres (dual-ORM committed rows)",
)

pytestmark = [_requires_pg, pytest.mark.django_db(transaction=True)]

SENSOR = "temperatureweather"
_T0 = datetime.datetime(2026, 6, 1, 10, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _align_signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", dj_settings.SECRET_KEY)


@pytest.fixture
def fast() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(django_user_model):
    return django_user_model.objects.create_user(
        username="own-admin",
        email="own-admin@example.com",
        password="irrelevant-3921",
        is_staff=True,
    )


def _auth(user):
    return {"Authorization": f"Bearer {AccessToken.for_user(user)}"}


def _mk_user_zone(django_user_model, tag):
    from apps.irrigation.models import Zone

    user = django_user_model.objects.create_user(
        username=f"own-{tag}", email=f"own-{tag}@x.com", password="pw-1"
    )
    zone = Zone.objects.create(
        user=user,
        name=f"own-{tag}-zone",
        space=1000.0,
        critical_moisture_threshold=20.0,
        pomp_flow_rate=1.0,
    )
    return user, zone


def _transferred_device(django_user_model, tag, values=(21.5, 22.0, 23.5)):
    """Commission a device under a technician, stamp readings with its
    ``device_id``, then transfer it to a client. Returns
    ``(tech, tech_zone, client, client_zone, device, readings)``.

    The reading rows keep the TECHNICIAN's ``user_id``/``zone_id`` snapshot —
    that is exactly the state that used to leak into the responses.
    """
    from apps.irrigation.models import Device
    from apps.sensors.models import TemperatureWeather

    tech, tech_zone = _mk_user_zone(django_user_model, f"{tag}-tech")
    client, client_zone = _mk_user_zone(django_user_model, f"{tag}-client")
    device = Device.objects.create(
        user=tech,
        zone=tech_zone,
        device_type="lora",
        serial=f"own-{tag}-serial",
        is_active=True,
    )
    readings = [
        TemperatureWeather.objects.create(
            user=tech,
            zone=tech_zone,
            value=v,
            timestamp=_T0 + datetime.timedelta(hours=i),
            device_id=device.id,
        )
        for i, v in enumerate(values)
    ]
    # TRANSFER — one row on analytics_device, no reading rewrite.
    Device.objects.filter(id=device.id).update(user=client, zone=client_zone)
    for r in readings:
        r.refresh_from_db()
        assert r.user_id == tech.id and r.zone_id == tech_zone.id
    return tech, tech_zone, client, client_zone, device, readings


def _list(fast, admin, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    r = fast.get(f"/admin/sensor-data?sensor={SENSOR}&{qs}", headers=_auth(admin))
    assert r.status_code == 200, r.text
    return r.json()


# ===========================================================================
# admin explorer — list projection
# ===========================================================================
def test_admin_list_finds_rows_of_a_device_transferred_to_the_client(
    fast, admin, django_user_model
):
    """The client's own data must be reachable by BOTH his username and his
    zone — filtering by the zone the response itself reports used to return 0
    rows, because the response reported the technician's zone."""
    _, _, client, client_zone, _, readings = _transferred_device(
        django_user_model, "list"
    )

    by_user = _list(fast, admin, username=client.username)
    assert by_user["count"] == len(readings)

    by_zone = _list(fast, admin, zone_id=client_zone.id)
    assert by_zone["count"] == len(readings)

    both = _list(fast, admin, username=client.username, zone_id=client_zone.id)
    assert {row["id"] for row in both["rows"]} == {r.id for r in readings}


def test_admin_list_round_trips_on_the_labels_it_returns(
    fast, admin, django_user_model
):
    """The exact ADM-2 loop: read a row's reported username/zone_id out of the
    response, filter on them, and get the SAME rows back. Any disagreement
    between filtering and projection makes this return nothing."""
    _, _, client, _, _, readings = _transferred_device(django_user_model, "trip")

    # the raw snapshot (the technician's zone) is not a valid filter key
    assert _list(fast, admin, zone_id=readings[0].zone_id)["count"] == 0

    seed = _list(fast, admin, username=client.username)
    assert seed["count"] == len(readings)
    label_user = seed["rows"][0]["username"]
    label_zone = seed["rows"][0]["zone_id"]

    echoed = _list(fast, admin, username=label_user, zone_id=label_zone)
    assert {row["id"] for row in echoed["rows"]} == {r.id for r in readings}


def test_admin_list_labels_rows_with_the_effective_owner_only(
    fast, admin, django_user_model
):
    """No response may ever name a user/zone other than the effective owner."""
    tech, tech_zone, client, client_zone, _, _ = _transferred_device(
        django_user_model, "label"
    )

    for params in (
        {"username": client.username},
        {"zone_id": client_zone.id},
        {"username": client.username, "zone_id": client_zone.id},
    ):
        payload = _list(fast, admin, **params)
        assert payload["count"] > 0
        for row in payload["rows"]:
            assert row["username"] == client.username
            assert row["zone_id"] == client_zone.id
            assert row["username"] != tech.username
            assert row["zone_id"] != tech_zone.id

    # ...and the previous owner keeps nothing.
    assert _list(fast, admin, username=tech.username)["count"] == 0
    assert _list(fast, admin, zone_id=tech_zone.id)["count"] == 0


def test_admin_patch_returns_the_effective_owner(fast, admin, django_user_model):
    _, tech_zone, client, client_zone, _, readings = _transferred_device(
        django_user_model, "patch"
    )
    r = fast.patch(
        f"/admin/sensor-data/{SENSOR}/{readings[0].id}",
        json={"value": 99.0},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["value"] == 99.0
    assert body["username"] == client.username
    assert body["zone_id"] == client_zone.id != tech_zone.id


# ===========================================================================
# admin explorer — range delete
# ===========================================================================
def test_admin_range_delete_matches_the_equivalent_list_exactly(
    fast, admin, django_user_model
):
    from apps.sensors.models import TemperatureWeather

    tech, tech_zone, client, client_zone, _, readings = _transferred_device(
        django_user_model, "del"
    )
    # A second, untouched owner whose rows must survive the delete.
    other, other_zone = _mk_user_zone(django_user_model, "del-other")
    survivor = TemperatureWeather.objects.create(
        user=other, zone=other_zone, value=5.0, timestamp=_T0
    )

    window = "from=2026-06-01T00:00:00%2B00:00&to=2026-06-02T00:00:00%2B00:00"
    listed = fast.get(
        f"/admin/sensor-data?sensor={SENSOR}"
        f"&username={client.username}&zone_id={client_zone.id}&{window}",
        headers=_auth(admin),
    )
    assert listed.status_code == 200, listed.text
    expected_ids = {row["id"] for row in listed.json()["rows"]}
    assert expected_ids == {r.id for r in readings}

    r = fast.delete(
        f"/admin/sensor-data/{SENSOR}"
        f"?username={client.username}&zone_id={client_zone.id}&{window}",
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "deleted", "deleted": len(expected_ids)}
    assert not TemperatureWeather.objects.filter(id__in=expected_ids).exists()
    assert TemperatureWeather.objects.filter(id=survivor.id).exists()


def test_admin_range_delete_on_the_previous_owner_deletes_nothing(
    fast, admin, django_user_model
):
    """The stale snapshot must not be a deletion key — scoping the delete to
    the technician the device WAS commissioned under must be a no-op."""
    from apps.sensors.models import TemperatureWeather

    tech, tech_zone, _, _, _, readings = _transferred_device(django_user_model, "nodel")
    window = "from=2026-06-01T00:00:00%2B00:00&to=2026-06-02T00:00:00%2B00:00"
    r = fast.delete(
        f"/admin/sensor-data/{SENSOR}"
        f"?username={tech.username}&zone_id={tech_zone.id}&{window}",
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "deleted", "deleted": 0}
    assert TemperatureWeather.objects.filter(
        id__in=[x.id for x in readings]
    ).count() == len(readings)


# ===========================================================================
# farmer-facing /sensors
# ===========================================================================
def test_hourly_readings_report_the_effective_owner(fast, django_user_model):
    tech, tech_zone, client, client_zone, _, readings = _transferred_device(
        django_user_model, "hourly"
    )
    r = fast.get(f"/sensors/{SENSOR}?zone={client_zone.id}", headers=_auth(client))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == len(readings)
    for row in rows:
        assert row["user"] == client.id != tech.id
        assert row["zone"] == client_zone.id != tech_zone.id


def test_raw_readings_report_the_effective_owner(fast, django_user_model):
    tech, tech_zone, client, client_zone, _, readings = _transferred_device(
        django_user_model, "raw"
    )
    r = fast.get(
        f"/sensors/{SENSOR}?zone={client_zone.id}&raw=true", headers=_auth(client)
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert {row["id"] for row in rows} == {x.id for x in readings}
    for row in rows:
        assert row["user"] == client.id != tech.id
        assert row["zone"] == client_zone.id != tech_zone.id


def test_patch_reading_authorises_via_the_effective_owner(fast, django_user_model):
    """The new owner may correct his own history; the previous owner may not."""
    tech, _, client, client_zone, _, readings = _transferred_device(
        django_user_model, "pat"
    )
    row_id = readings[0].id

    ok = fast.patch(
        f"/sensors/{SENSOR}", json={"id": row_id, "value": 42.0}, headers=_auth(client)
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["value"] == 42.0
    assert body["user"] == client.id
    assert body["zone"] == client_zone.id

    denied = fast.patch(
        f"/sensors/{SENSOR}", json={"id": row_id, "value": 7.0}, headers=_auth(tech)
    )
    assert denied.status_code == 404
    assert denied.json() == {"error": "Not found"}
