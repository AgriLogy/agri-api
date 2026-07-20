# CHANGELOG


## v1.118.0 (2026-07-20)

### Features

- **observability**: Health + error alerts to Discord (+ disk/container beacon)
  ([#411](https://github.com/AgriLogy/agri-api/pull/411),
  [`a37f3f9`](https://github.com/AgriLogy/agri-api/commit/a37f3f9a6b9595a7eb4728363ab200d0064a4f4d))

Closes #410


## v1.117.0 (2026-07-20)

### Features

- **observability**: Route Resend-429 alert to Discord
  ([#409](https://github.com/AgriLogy/agri-api/pull/409),
  [`d7fc600`](https://github.com/AgriLogy/agri-api/commit/d7fc600f063df0dba212077c8dc84b46ccf01b83))

Closes #408


## v1.116.1 (2026-07-20)

### Bug Fixes

- **observability**: Drop duplicate uvicorn.access log + default Grafana host port to 3300
  ([#407](https://github.com/AgriLogy/agri-api/pull/407),
  [`2752585`](https://github.com/AgriLogy/agri-api/commit/27525859e5c50cbd83c4661bf5263889af70e7da))

Closes #406


## v1.116.0 (2026-07-20)

### Features

- **observability**: Structured JSON logging + Loki/Grafana log stack
  ([#405](https://github.com/AgriLogy/agri-api/pull/405),
  [`a8e0b16`](https://github.com/AgriLogy/agri-api/commit/a8e0b16bda3b51aba356d8ba2a8f2adffe98899b))

Closes #404


## v1.115.0 (2026-07-17)

### Features

- **admin**: Expose a true Clients count on overview KPIs
  ([#401](https://github.com/AgriLogy/agri-api/pull/401),
  [`2ac9e96`](https://github.com/AgriLogy/agri-api/commit/2ac9e96b40a519f51791c253c7ba8255c3ca07cf))

Closes #400 · pairs with agri-admin ADM-1 (#56) — **merge together**.

## Why The dashboard's 'active users' KPI counts every enabled account (staff + technicians
  included), so the business couldn't see its real customer count — the root of ADM-1 ('active
  clients shows 1, should be 5').

## What Adds `clients_total` and `clients_active` to `GET /admin/overview`: - **client = non-staff,
  non-technician** account — the exact population `/admin/analytics` already calls 'customers'
  (matches `tasks_scan.py`'s customer filter). - Updated in **both** the live `fastapp` router and
  its django-ninja source (`apps/irrigation/router_admin.py`) so the byte-parity test stays green
  (identical keys + order). - `test_admin_overview.py` asserts the new keys.

## Note on the assumption 'Client' here = customer account. If you meant something else by 'active
  client' (e.g. recent login, or has an active device), tell me and I'll adjust the definition —
  it's a one-line query change.

Postgres tests run in CI (dual-ORM tests can't run locally).


## v1.114.0 (2026-07-12)

### Features

- **users**: Self-service profile + change-password on /users/me
  ([#397](https://github.com/AgriLogy/agri-api/pull/397),
  [`74f5a70`](https://github.com/AgriLogy/agri-api/commit/74f5a70c3d4662652a7e6377e97365560d8384e7))

Closes #396.

Backs the new agri-web Profile settings page (**mks-zakaria/agri-web#54**, ticket
  **AgriLogy/agrilogy-front#15**) by extending the caller's self endpoints. The frontend is wired to
  the live **fastapp** surface, so the real change is in `fastapp/routers/selfreads.py`; the
  Django-ninja original (`apps/users/router_admin.py`) is updated in lockstep to keep the strangler
  byte-parity intact.

## What changed - **`GET /users/me`** now returns `email`, `phone_number`, `first_name`, `last_name`
  alongside `username`/`preferred_language`/`notify_every`. `first_name`/`last_name` are the wire
  names for the model's `firstname`/`lastname` columns. - **`PATCH /users/me`** accepts those fields
  (all optional): - email format check + **uniqueness** (rejects if another user owns it) → 400 bare
  field-map `{"email": "This email is already in use."}`, - basic phone length check, - only
  supplied fields are written; existing `preferred_language` behaviour unchanged. - **`POST
  /users/me/change-password`** (new) — body `{current_password, new_password}`. Verifies
  `current_password` against the stored **Django pbkdf2 hash** and rejects a wrong one; runs the
  standard validators (min length 8 + common + numeric); on success sets the new password and
  returns `{"detail": "Password updated."}`.

## Password hashing Reuses `fastapp.passwords` (`verify_password` / `make_password` /
  `validate_password`) — the same Django-`check_password`-compatible pbkdf2_sha256 mechanism the
  technician-create and admin password-reset paths already use. No hand-rolled hasher, so stored
  hashes stay Django-compatible.

## Tests Added to `fastapp/tests/test_selfreads_parity.py`: GET returns the new fields
  (byte-identical across both surfaces), PATCH profile update persists, email-uniqueness +
  invalid-email rejection (byte-identical 400 maps), change-password happy path (new hash verifies
  via Django `check_password`), wrong-current-password rejection, too-short rejection. Verified
  green against a local Postgres (the full `test_selfreads_parity` + `test_users_parity` +
  `apps/users` suites: 146 passed); dual-ORM so CI runs them on Postgres too. ruff check + format
  clean.

Unblocks the agri-web profile page.


## v1.113.0 (2026-07-12)

### Features

- **devices**: Retire the backfill (history follows the device via the JOIN, phase 4)
  ([#389](https://github.com/AgriLogy/agri-api/pull/389),
  [`a4aba7c`](https://github.com/AgriLogy/agri-api/commit/a4aba7c318afa37e0908dee5cd049b08fb4fdce4))

Closes #388 · phase 4 (final) of device-keyed ownership

With P3 live, a transfer is just a one-row `analytics_device` UPDATE and the device's whole history
  follows via the JOIN — the backfill is obsolete. - `bulk_assign` + `patch_device`: plain UPDATE,
  no backfill enqueue / prior-zone capture - deleted `backfill_device_readings` (task + celery
  registration) - `backfill` request field kept **accepted-but-ignored** (deprecated) so the
  currently-deployed admin doesn't 422 before its own P4 ships - pruned the backfill tests

Full fastapp suite green (392). Pairs with agri-admin (drop the migrate toggle).


## v1.112.0 (2026-07-12)

### Features

- **sensors**: Flip reads to device-JOIN ownership resolution (device-keyed, phase 3)
  ([#387](https://github.com/AgriLogy/agri-api/pull/387),
  [`65cf200`](https://github.com/AgriLogy/agri-api/commit/65cf200dae0d0675c99c76122ba154b381ff15d5))

Closes #386 · phase 3 of device-keyed ownership (agri-core 0.21.0)

The read flip. A device transfer (one-row `analytics_device` update) now instantly moves its whole
  graph history — **no reading rewrite, no backfill**. - agri-core 0.20.0 → 0.21.0 (device-JOIN in
  hourly_averages/average/sum/latest_reading + alert reads) - `fastapp/sensors.py`: `raw_readings` +
  `hourly_readings` sample query device-aware - `admin_sensor_data.py`: explorer filter device-aware

**Safe to deploy now** — the live pH device has `device.zone == row.zone`, so no visible change; the
  payoff is instant future transfers. Non-device readings unchanged (COALESCE fallback).

Tests: 3 new (transfer moves graph with zero rewrite / raw follows / weather unaffected); full
  fastapp suite green (397).


## v1.111.0 (2026-07-12)

### Features

- **ingest**: Dual-write device_id on LoRa readings (device-keyed ownership, phase 1)
  ([#385](https://github.com/AgriLogy/agri-api/pull/385),
  [`08a78c0`](https://github.com/AgriLogy/agri-api/commit/08a78c0b5994b33ef293323339ca3814293cb67c))

Closes #384 · phase 1 of device-keyed ownership (agri-db #59 / agri-core 0.20.0)

Stamps `device_id` on every LoRa reading; **reads are unchanged** this phase (the query flip is
  phase 3).

- agri-core pin 0.19.0 → 0.20.0 (brings the agri-db 0.15.0 `device_id` column) - Django
  `_ReadingBase` adds `device_id` to the 37 reading models + migration `0067` (config models
  SensorColor/SensorLocation/UserSensorUnitPreference stay on `_SensorBase`) - fastapp
  `resolve_device` → `(device_id, user, zone)`; `write_reading` + `handle_chirpstack_uplink` stamp
  it on every reading (incl. the lora fallback, so it follows on assignment); `user_id`/`zone_id`
  still written as snapshot - exclude `device_id` from the public sensor serializer (keeps contract
  + parity)

Tests: 2 new stamping tests; full fastapp suite green (394) on local Postgres; sensor parity
  restored.

## ⚠️ DO NOT MERGE/DEPLOY until the live `agrydata` DB has the `device_id` columns The additive `ADD
  COLUMN`s must be applied to `agrydata` first (staged SQL, run by the user), else ingest INSERTs
  fail on a missing column.


## v1.110.0 (2026-07-12)

### Features

- **devices**: Support backfill on the single-device Edit (PATCH)
  ([#383](https://github.com/AgriLogy/agri-api/pull/383),
  [`d6ba3f3`](https://github.com/AgriLogy/agri-api/commit/d6ba3f393c53458f0519e20a260a74a72ec0a670))

Closes #382 · follow-up to #379/#381

Operators naturally transfer a captor via the **Edit** drawer, which changed ownership but never ran
  the backfill — leaving history behind. This adds an opt-in migrate on `PATCH /devices/{id}`:

- transient `backfill` flag on the payload; - when the edit moves the device to a real zone (owner
  or zone changed) and `backfill` is set, enqueue `backfill_device_readings` with the device's
  **prior** (user, zone) as source — identical task/behavior to bulk-assign; - no-op when the edit
  doesn't move the device.

Tests: PATCH enqueues on transfer with the right source/target; no enqueue without the flag or on
  same-zone. Full `fastapp` suite green (392). PATCH response byte-parity unchanged (device parity
  tests pass). Pairs with agri-admin (Edit-drawer toggle).


## v1.109.1 (2026-07-12)

### Bug Fixes

- **devices**: Migrate a device's full history from its prior zone on transfer
  ([#381](https://github.com/AgriLogy/agri-api/pull/381),
  [`5878d04`](https://github.com/AgriLogy/agri-api/commit/5878d04d73924154ac4ef88e30d9c914f95b2642))

Closes #380 · follow-up to #379

## Why

Captors are commissioned under a **technician** account, then transferred to the **client** — who
  must see the device's full history from day one. The initial backfill missed this: it only
  migrated from the shared `lora` zone, and matched readings to raw uplinks by timestamp (leaving
  older, uplink-less readings behind). This is exactly the "I still see data in the old account"
  symptom.

## Changes

- **`bulk_assign`** now captures each device's **prior** `(user_id, zone_id)` before reassigning and
  forwards it to the backfill as `source_user_id`/`source_zone_id`. - **`backfill_device_readings`**
  migrates from that source (a technician zone, or the `lora` catch-all when the device was
  unassigned) and: - does a **complete move** of all the source zone's ph/battery/signal readings
  when that zone holds **only this one device** (the common commissioning case — moves the *entire*
  history, including readings with no uplink row); - falls back to **timestamp-correlation** only
  when the source zone is shared by several devices (so it never steals another device's data). -
  Returns `mode: full|correlated`. Idempotent; no-op on same-zone.

## Tests

New: full-move from a technician zone incl. a reading with no uplink row; correlated fallback for a
  shared source zone. Updated the enqueue test to assert the source is forwarded. Full `fastapp`
  suite green (390 passed) on local Postgres.

## Note

The single-device **Edit** drawer still doesn't backfill — transfers should use **Assign selected**
  (which has the *migrate past readings* toggle). No schema change.


## v1.109.0 (2026-07-12)

### Documentation

- **mqtt**: Authoritative end-to-end MQTT ingest knowledge
  ([#377](https://github.com/AgriLogy/agri-api/pull/377),
  [`f372edc`](https://github.com/AgriLogy/agri-api/commit/f372edc4012f10a2b436ab8b4ffab2c134939a1c))

Closes #376

Adds **`docs/flows/mqtt-ingest.md`** — one authoritative doc for the whole MQTT path, spanning
  agri-api (subscriber) + agri-bridge (publisher).

### Contents - **Diagrams (Mermaid):** full flow + happy-path sequence, using the real function
  names. - **Topic + payload contract:** the four subscribed filters and their bodies. -
  **Components with `file:line`:** the bridge publisher (`createMqttPublisher`, dual-publish),
  ChirpStack native MQTT, the subscriber (`MqttIngest.run/_on_connect/_guard/_on_*`), the shared
  `fastapp.ingest` handlers (`handle_chirpstack_uplink`, `handle_metrics`, `write_reading`,
  `dispatch_alerts_for_reading`, …), the HTTP webhooks, and the read/graph path (`list_readings` →
  `hourly_readings`). - **Config** for both repos, **deploy topology** (`mqtt` role +
  `agri-api-mqtt` service + dev mosquitto), the **production activation runbook** (incl. disabling
  the ChirpStack HTTP integration to avoid double-delivery), **testing/CI** (the `mqtt-e2e` gate +
  `MQTT_REQUIRE_BROKER`), **gotchas**, **rollback**, and the deferred follow-ups.

### Also - Wired into `docs/INDEX.md`. - Cross-linked from `docs/flows/data-ingestion.md` (the older
  HTTP-era doc) with a note that the live HTTP ingest now runs on `fastapp`.

Docs-only — no code change, no release.

### Features

- **ingest**: Attribute LoRaWAN uplinks to device-owner accounts
  ([#379](https://github.com/AgriLogy/agri-api/pull/379),
  [`b38af82`](https://github.com/AgriLogy/agri-api/commit/b38af822e3f73acd7142a2e8a7d9a9a369b68135))

Closes #378

## What

Route each ChirpStack/LoRaWAN uplink to the account/zone that **owns** the device, instead of the
  single shared `lora` catch-all. Groundwork already existed (the `analytics_device` table +
  `/devices` CRUD); this wires it into the ingest path and adds bulk attribution.

## Changes

- **`resolve_device_zone(session, dev_eui)`** (`fastapp/ingest.py`) — looks up `analytics_device` by
  serial(=DevEUI). Registered + active + assigned → the owner's `(user_id, zone_id)`; otherwise
  `None` → the `lora` fallback (byte-parity with prior behavior). Replaces the hardcoded
  `ensure_lora_zone` in `handle_chirpstack_uplink`. - **Auto-registration** — an unknown DevEUI is
  lazily inserted as an *unassigned* device (owned by the `lora` placeholder, `zone_id` NULL) via
  `INSERT … ON CONFLICT DO NOTHING`, so every device surfaces in the admin list on first uplink
  without disrupting routing. - **`POST /devices/bulk-assign`** (`fastapp/routers/devices.py`,
  staff-only) — attribute many devices to one account+zone in a call; reuses `_resolve_owner_zone`
  for validation; returns `assigned`/`failed` per id; optionally enqueues backfill. -
  **`backfill_device_readings`** Celery task (`fastapp/tasks_devices.py`) — migrates a device's past
  readings from the `lora` zone to its new zone, correlating readings to the device via
  `lora_uplink.dev_eui` (readings carry no DevEUI). Idempotent; exact for a single-device `lora`
  zone, best-effort when multiple devices are mixed.

## Tests

`test_device_routing.py` (5) + `test_device_bulk_assign.py` (8) — routing across
  registered/assigned/unassigned/inactive/unknown, auto-register idempotency, bulk-assign
  happy/partial/403/validation/not-owned/backfill-enqueue, and the backfill move + idempotency. Full
  `fastapp` suite green (388 passed) against local Postgres; chirpstack parity + MQTT e2e
  unaffected.

## Notes

- No DB migration — `analytics_device` already exists; this is code + data only. - The legacy Django
  chirpstack route is unchanged (all ingest traffic is on fastapp); parity tests exercise only the
  unregistered→fallback path, so they stay green.

### Testing

- **ingest**: Real-broker MQTT end-to-end + broker tests, gated in CI
  ([#375](https://github.com/AgriLogy/agri-api/pull/375),
  [`85e1ab7`](https://github.com/AgriLogy/agri-api/commit/85e1ab7aeaec04f17f30bbf753d99a5e536a53a5))

Closes #373

> Stacked on #372 (base = `fix/lora-user-notify-defaults`). Review/merge that first; this branch
  retargets to `main` after.

## What Two test tiers over a **real mosquitto broker** (the existing `test_mqtt_ingest` mocks paho
  entirely):

- **`test_mqtt_broker.py`** — real paho publish → subscriber network thread → the right
  `fastapp.ingest` handler (recorded), no DB. Proves the socket plumbing per topic + malformed-drop
  + liveness. - **`test_mqtt_e2e.py`** — real broker + **real Postgres + unmocked handlers**. Every
  topic and branch asserted against DB rows + the same alert enqueue the HTTP path makes: -
  ChirpStack data frame → LoraUplink + pH/battery/signal rows - ChirpStack status frame (fPort 5) →
  uplink only, no metric rows - weather multi-metric, single sensor,
  **single-sensor-triggers-alert** - Bivocom (bridge-shaped), unknown user, unknown sensor_key,
  malformed - negative cases use a trailing **sentinel** so "no row written" is deterministic.

Shared `conftest.py` fixtures launch a throwaway `mosquitto` on an ephemeral port and SKIP when it
  is absent.

## CI gate New **`mqtt-e2e`** job (Postgres 16 service + `apt-get install mosquitto`) runs both
  files for real on every PR + push. Sets `MQTT_REQUIRE_BROKER=1` so a missing broker **fails** the
  job rather than silently skipping — the gate can never quietly no-op.

## Verified locally - Full `src/` suite: **923 passed** against real mosquitto + Postgres; total
  coverage **90.9%** (floor 85). - The e2e caught a real latent bug → fixed in #372.


## v1.108.1 (2026-07-08)

### Bug Fixes

- **ingest**: Provision the lora user with explicit notification defaults
  ([#374](https://github.com/AgriLogy/agri-api/pull/374),
  [`6f06fa9`](https://github.com/AgriLogy/agri-api/commit/6f06fa93899f6aa9149bee2e2280ab79b63d7c8a))

Closes #372

## What `ensure_lora_zone` lazily creates the `lora` CustomUser via SQLAlchemy, omitting
  `notify_every` / `preferred_language` and relying on a DB **server default**. That default exists
  in the agri.db (Alembic) schema but NOT in a Django-migration-built schema (the test DB, and any
  non-Alembic bootstrap), so the insert raises `NotNullViolation` the first time the `lora` user is
  provisioned over a non-Django ingest path.

The HTTP ChirpStack route never hit this because Django (`get_or_create`) created the row first; the
  new MQTT path has no Django call.

## Fix Set `notify_every=240` and `preferred_language="fr"` explicitly — byte-for-byte what Django's
  `get_or_create` writes — so provisioning works on any schema.

## Test Regression coverage lands in the stacked e2e PR (#373): its ChirpStack case exercises
  first-time `lora` provisioning and failed before this fix.


## v1.108.0 (2026-07-07)

### Features

- **ingest**: Consume sensor data over MQTT (ChirpStack + generic + Bivocom)
  ([#371](https://github.com/AgriLogy/agri-api/pull/371),
  [`9ca4806`](https://github.com/AgriLogy/agri-api/commit/9ca4806a55b3b0233cdc1e804a412de0aa94e41d))

Closes #369

> Stacked on #368 (base = `refactor/ingest-shared-handlers`). Review/merge that first; this branch
  retargets to `main` after.

## What A persistent MQTT subscriber — new `mqtt` entrypoint role (`docker-entrypoint.sh mqtt` →
  `fastapp.mqtt`) — that consumes sensor data over MQTT and writes through the SAME `fastapp.ingest`
  handlers the HTTP webhooks use. No new business logic, no schema change.

| Topic | Source | Handler | |---|---|---| | `application/+/device/+/event/up` | ChirpStack v4 |
  `handle_chirpstack_uplink` | | `agrilogy/+/weather` | generic multi-metric | `handle_metrics` | |
  `agrilogy/+/sensor/+` | generic single reading | `handle_metrics` | | `agrilogy/+/bivocom` |
  Bivocom (bridge-shaped) | `handle_metrics` |

## Design - **Additive.** HTTP `/ingest/*` keeps working; rollback = stop the container. - **Broker
  = ChirpStack's own** (LoRaWAN uplinks land there natively). Dev uses a bundled `mosquitto`; the
  droplet sets `MQTT_HOST`/creds in `back/.env`. - New `agri-api-mqtt` compose service. ⚠️ **Single
  instance only** — every subscriber gets every message. - Bad payloads are logged and dropped
  (never crash the loop); `loop_forever` auto-reconnects. - Broker-level auth (user/pass/TLS)
  instead of the still-TODO webhook shared-secret.

## Out of scope (follow-up) Raw-Modbus-tag Bivocom via `analytics_devicesensor` — blocked on the
  held `00a3976cb808` migration, the table having no `user_id`, and the string/int `device_id`
  mismatch. The bridge-shaped path here already covers today's live Bivocom data.

## Test - `pytest src/fastapp/tests/test_mqtt_ingest.py` — 15 pass: topic→handler routing, payload
  parsing, malformed-drop, and paho filter/topic matching (broker-side wiring proof without a
  broker). - Row/alert parity of the shared handlers: `test_ingest_parity` (from #368). - `docker
  compose config` valid; ruff clean.

### Refactoring

- **ingest**: Extract transport-agnostic handle_* handlers
  ([#370](https://github.com/AgriLogy/agri-api/pull/370),
  [`a15dd8d`](https://github.com/AgriLogy/agri-api/commit/a15dd8de07f3cb473d02e4d4a738e4bc4bf8888a))

Closes #368

## What Lift the device-webhook persist + alert-dispatch bodies out of the HTTP route functions in
  `back/src/fastapp/routers/ingest.py` into `back/src/fastapp/ingest.py`:

- `handle_chirpstack_uplink(session, ...)` — raw-uplink store + pH/battery/signal rows under the
  `lora` zone + alert dispatch - `handle_metrics(session, *, client, metrics, timestamp=None)` —
  user→zone resolution + per-`sensor_key` write + alert dispatch (shared by weather + single-sensor)
  - `resolve_user_zone(...)` + `IngestError` — uniform client/zone errors

The `/ingest/*` routes are now thin: parse → one handler → shape response.

## Behaviour **Unchanged.** Response envelopes, status codes, persisted rows, and enqueued alert
  tasks are byte-identical — `test_ingest_parity` is untouched and still green.

## Why Prep so a second ingest transport (MQTT, #369) reuses the exact same write path with zero
  duplication.

## Test - `pytest src/fastapp/tests/test_ingest_parity.py` (dual-ORM, Postgres — CI). - Full fastapp
  suite collects + passes (324 skip without Postgres, 0 fail).


## v1.107.1 (2026-07-06)

### Bug Fixes

- **weather**: Source Open-Meteo coords from picked location / zone
  ([#367](https://github.com/AgriLogy/agri-api/pull/367),
  [`553d0fe`](https://github.com/AgriLogy/agri-api/commit/553d0fe9ebb59c1f4745dddad691c79197a73e78))

## Problem The Open-Meteo reference curve + comparison line are empty for nearly all farmers.
  **Verified on prod:** `et0_openmeteo_mm: null` on every day, `/weather/et0-series` → 0 points.
  Cause: the endpoints read coordinates from `user.latitude/longitude`, which is `None` on most
  accounts — the weather location picker saves client-side (localStorage) and calls Open-Meteo from
  the browser; it never sets server coords (those are admin-only).

## Fix `/weather/et-forecast` + `/weather/et0-series` now accept optional `lat`/`lon` and resolve
  coordinates in priority order: 1. **farmer's picked location** (frontend passes it from
  `readWeatherLocation()`) 2. account `lat/lon` 3. **zone** weather coordinates
  (`humidity_weather_latitude/longitude`, non-null on every zone)

Mock/FAO bars unchanged (still account coords). Best-effort fetch unchanged.

Paired with agri-web (frontend passes the picked location).

Closes #366


## v1.107.0 (2026-07-06)

### Features

- **weather**: Add /weather/et0-series (Open-Meteo daily ET₀ over a range)
  ([#365](https://github.com/AgriLogy/agri-api/pull/365),
  [`be75272`](https://github.com/AgriLogy/agri-api/commit/be75272aa002b3c56327738a4d82b6715b7532ce))

## What New owner-scoped `GET /weather/et0-series?zone=&start_date=&end_date=` returning
  **Open-Meteo's real published FAO-56 ET₀** (mm/day) per day across the window, shaped like a
  sensor-reading series (`[{timestamp, value, default_unit, available_units}]`, stamped at noon).

## Why The station ET₀ chart's two series are effectively unfeedable right now (`et0weather` has no
  writer; `et0calculated` needs full weather-sensor coverage). This gives the chart a **real**
  reference line with zero hardware. agri-web plots it as a third line (separate PR).

## Safety Reuses the keyless best-effort `fetch_openmeteo_et0`; `[]` on
  unreachable/out-of-window/no-lat-lon. Range capped at 31 days. Owner-scoped 404 identical to
  `/weather/et-forecast`.

## Tests Route tests: 200 + list shape, missing-zone 404, other-user's-zone 404. ruff + format
  clean.

Closes #364


## v1.106.0 (2026-07-06)

### Features

- **weather**: Add a real Open-Meteo reference ET₀ curve
  ([#363](https://github.com/AgriLogy/agri-api/pull/363),
  [`ebc4043`](https://github.com/AgriLogy/agri-api/commit/ebc40431e88c0682f8ee37024c65b1baf1024028))

## What `GET /weather/et-forecast` now returns **Open-Meteo's own published FAO-56 reference ET₀**
  (`et0_fao_evapotranspiration`) per day as `et0_openmeteo_mm`, plus top-level `reference_provider:
  "open-meteo"`. The frontend plots this as a real reference **curve** next to the computed bars.

## Why The bars are still the deterministic mock; this introduces a genuine, **keyless** data source
  so users see real reference ET₀ alongside the placeholder — and it goes fully real the moment we
  trust it for the bars too.

## Safety Best-effort fetch (stdlib `urllib`, ~4s timeout): missing lat/lon, timeout, network error,
  or `ET0_OPENMETEO=off` → empty map → `et0_openmeteo_mm` null on every day, curve absent. **Never
  fails the endpoint.** Live by default (`ET0_OPENMETEO=on`); the droplet already allows outbound
  HTTPS.

## Tests - `test_openmeteo_provider.py` — network-free unit tests (parse/round, disable switch,
  missing-coords, network-error fallback, null-skip). Logic verified locally + against the live
  Open-Meteo API. - `test_weather_parity.py` — updated: fastapp is now an intentional superset of
  the legacy (unrouted) Django endpoint; shared F2 contract still compared, new fields asserted
  separately.

Closes #362


## v1.105.2 (2026-07-05)

### Bug Fixes

- **fastapp**: Route on-demand send_task to the agriapi queue
  ([#361](https://github.com/AgriLogy/agri-api/pull/361),
  [`804efba`](https://github.com/AgriLogy/agri-api/commit/804efbabea460a238af8ed45bdc466e78453fd1b))

Closes #360

The fastapp on-demand enqueue helper had no routing, so `send_task("agriapi.tasks.…")` landed on the
  default `celery` queue — the `-Q agriapi` worker never consumed the on-demand alert /
  zone-outbound tasks (silently dropped since the /ingest cutover; surfaced while verifying the F10
  worker cutover — they hit the legacy default-queue worker as 'unregistered'). Fix mirrors Django's
  `CELERY_TASK_ROUTES` (`agriapi.*` → `agriapi`) + `task_default_queue`. 2 tests.

### Continuous Integration

- Cut Celery worker + beat over to the native fastapp app (F10)
  ([#359](https://github.com/AgriLogy/agri-api/pull/359),
  [`055db05`](https://github.com/AgriLogy/agri-api/commit/055db05809ebd29afc7c98833affbf329c983840))

Closes #358

Strangler **F10 cutover** — switch `agri-api-worker`/`agri-api-beat` from the Django Celery app to
  the native Django-free app (`fast-worker`/`fast-beat`). All 13 task bodies parity-tested + run
  live against prod data; native worker boots + connects to Redis; static beat cadences match the
  live `PeriodicTask` rows. Deploy recreates the two containers (old stops before new starts → no
  double-consume of the shared `agriapi` queue). **Rollback** = set commands back to `worker`/`beat`
  and redeploy.


## v1.105.1 (2026-07-05)

### Bug Fixes

- **fastapp**: Static beat cadences match the live PeriodicTask rows (F10b)
  ([#357](https://github.com/AgriLogy/agri-api/pull/357),
  [`352b711`](https://github.com/AgriLogy/agri-api/commit/352b711f3252ee70919883ab2dfcd0669afd33f4))

Closes #356

Recon for the F10b beat cutover found the live prod cadences (DatabaseScheduler `PeriodicTask` rows)
  are the *dev* crontabs — the prod `CELERY_BEAT_SCHEDULE` code branch never took effect. Corrects
  the static `beat_schedule` to the live cadences (`*/4`, `*/10`, `*/2`) so switching the beat
  container to the static PersistentScheduler preserves current behaviour. Adds a test pinning each
  cadence to the live value.


## v1.105.0 (2026-07-05)

### Features

- **deploy**: Add fast-worker / fast-beat entrypoint roles (F10b-prep)
  ([#355](https://github.com/AgriLogy/agri-api/pull/355),
  [`9c442a3`](https://github.com/AgriLogy/agri-api/commit/9c442a305474b4d1c8bd84e0eee414e8157ae182))

Closes #354

Strangler **F10b prep** — `fast-worker` + `fast-beat` entrypoint roles running the native
  Django-free Celery app (`celery -A fastapp.celery_app`). **Additive**: defined but not run — the
  live compose still uses the Django `worker`/`beat`. The actual container cutover is a separate
  careful deploy step, only after diffing the static beat schedule vs the live `PeriodicTask` rows.
  The two workers must never run together (shared `agriapi` queue → double execution); rollback =
  switch the compose command back.


## v1.104.0 (2026-07-05)

### Features

- **fastapp**: Native Celery app registering all tasks + beat (F10a)
  ([#353](https://github.com/AgriLogy/agri-api/pull/353),
  [`7bb7cbe`](https://github.com/AgriLogy/agri-api/commit/7bb7cbefd55d7b26e2d061af3d3a1bafe3489f43))

Closes #352

Strangler **F10a** — `fastapp/celery_app.py`, a native (Django-free) Celery app that registers all
  13 ported task bodies under the same wire-contract names (`agriapi.tasks.<name>`), a static
  `beat_schedule` mirroring the prod cadences, and fail-soft `analytics_taskrun` recording from
  signals. Same broker + `agriapi` queue + routing as `agriapi.celery` so both apps interoperate
  during overlap.

**Additive** — only creates the app; NO container is switched. The worker/beat cutover (swap
  `docker-entrypoint.sh` to `celery -A fastapp.celery_app`) is the careful **F10b** step, after
  diffing this static schedule against the live `PeriodicTask` rows (prod ran the
  DatabaseScheduler).

4 tests: 13 names registered, tasks wrap the ported bodies, beat matches prod set (simulate
  excluded), queue/routing/timezone match Django. Full suite **334 passed**.


## v1.103.0 (2026-07-05)

### Features

- **fastapp**: Port run_due_irrigation_programs Celery task (F8b — completes F8)
  ([#351](https://github.com/AgriLogy/agri-api/pull/351),
  [`e45326f`](https://github.com/AgriLogy/agri-api/commit/e45326fec61664b8d985cb178714feb92d64ee2d))

Closes #350

Strangler **F8b (final)** — the irrigation-program scheduler moves to `fastapp/tasks_scan.py`. Fires
  enabled programs whose start_time is in the just-passed window today, once per window (weekday
  filter + atomic dedup on `last_run_at`), creates a scheduled `OutputCommand`, and dispatches it
  (simulated unless `IRRIGATION_DISPATCH_ENABLED` — the safe default; port of
  `output_dispatch.dispatch_command`). Additive until F10.

2 tests: fire + dedup parity vs Django (same fired/skipped + OutputCommand rows + `last_run_at`
  stamp), none-due no-op. Full suite **330 passed**.

**This completes F8** — all 13 Celery task bodies now have Django-free fastapp implementations.
  Next: **F10** (native `fastapp/celery_app.py` worker + static beat + container switch).


## v1.102.0 (2026-07-05)

### Features

- **fastapp**: Port scan_proactive_insights Celery task (F8b)
  ([#349](https://github.com/AgriLogy/agri-api/pull/349),
  [`34f57b6`](https://github.com/AgriLogy/agri-api/commit/34f57b638690977857fd65f79cb4380e61264045))

Closes #348

Strangler **F8b (part 5)** — the proactive-insights scan moves to `fastapp/tasks_scan.py`. Per
  active customer, compute an irrigation insight via the F7-ported assistant tool
  (`_get_irrigation_advice` with an `AuthedUser`) and email a nudge when a zone needs water; deduped
  once per cooldown window via an atomic claim on `AssistantProactiveNotice.last_sent` (rolled back
  on send failure). Additive until F10.

2 tests: scan + notify parity vs Django (advice mocked, filter + claim run for real → same
  scanned/notified/quiet/skipped + emails + delivery rows), cooldown dedup. Full suite **328
  passed**.

Remaining F8b: `run_due_irrigation_programs` → then F10 native worker.


## v1.101.0 (2026-07-05)

### Features

- **fastapp**: Port scan_device_health Celery task (F8b)
  ([#347](https://github.com/AgriLogy/agri-api/pull/347),
  [`fac7a13`](https://github.com/AgriLogy/agri-api/commit/fac7a133ae54566a12b207fbb918be4ede75763d))

Closes #346

Strangler **F8b (part 4)** — the device-health scan beat task moves to a Django-free
  `fastapp/tasks_scan.py`. Scans active devices for offline / low-battery (health from latest
  `lora_uplink` via raw SQL), emails the owner once per cooldown window (atomic dedup claim on
  `last_health_notified`, rolled back on send failure). Pure `classify_device_health` ported
  verbatim. Additive until F10.

3 tests: classify + notify parity vs Django (same scanned/notified/healthy/skipped + emails +
  delivery rows + claim stamps), cooldown dedup, pure classifier. Full suite **326 passed**.

Remaining F8b: `scan_proactive_insights`, `run_due_irrigation_programs` → F10 native worker.


## v1.100.0 (2026-07-05)

### Features

- **fastapp**: Port send_periodic_notifications Celery task (F8b)
  ([#345](https://github.com/AgriLogy/agri-api/pull/345),
  [`1fc1de7`](https://github.com/AgriLogy/agri-api/commit/1fc1de70a98c20f962349e6de8913986b31aade7))

Closes #344

Strangler **F8b (part 3)** — the cadence-gated periodic field-status digest moves to a Django-free
  fastapp function. Sends to each active user with an email once their `notify_every`-minute cadence
  elapses; the atomic cadence claim (conditional UPDATE stamping `last_notified` up-front to close
  the double-send race, #180) is ported to SQLAlchemy. Composition reuses
  `agri.core.notifications.compose_notification_for_user`. Additive until F10.

2 parity tests: gating + sent/skipped/failed + delivery rows + `last_notified` stamp vs Django
  (composer/email mocked, the claim runs for real on both), and the none-due no-op. Full suite **323
  passed**.

Remaining F8b: `scan_device_health`, `scan_proactive_insights`, `run_due_irrigation_programs` → F10.


## v1.99.0 (2026-07-05)

### Features

- **fastapp**: Port flag_idle_zones Celery task (F8b)
  ([#343](https://github.com/AgriLogy/agri-api/pull/343),
  [`385ce29`](https://github.com/AgriLogy/agri-api/commit/385ce29bc3593165fd374d653c32222bf38ddd4f))

Closes #342

Strangler **F8b (part 2)** — the idle-zone liveness beat task moves to a Django-free fastapp
  function. Flags a zone whose newest reading across all sensor models is older than
  `ZONE_IDLE_THRESHOLD_HOURS` and emails the owner; skips fresh + never-reported zones. The reflag
  throttle (Django LocMem `cache.add`) becomes a monkeypatchable **Redis `SET NX EX`** (fail-closed
  on Redis error → no spam). Additive until F10.

4 tests: flagging parity vs Django (throttle+email mocked both sides → same flagged count / email /
  delivery row), none-idle no-op, no-recipient-consumes-slot, and the throttle's claim-once
  semantics. Full suite **321 passed**.

Remaining F8b: `send_periodic_notifications`, `scan_device_health`, `scan_proactive_insights`,
  `run_due_irrigation_programs` → F10 native worker.


## v1.98.0 (2026-07-04)

### Features

- **fastapp**: Port compute_et0_vpd_hourly Celery task (F8b)
  ([#341](https://github.com/AgriLogy/agri-api/pull/341),
  [`36fcea9`](https://github.com/AgriLogy/agri-api/commit/36fcea97f44788c1d40e452c661794b852922390))

Closes #340

Strangler **F8b (part 1)** — the hourly ET0/VPD persistence beat task moves to a Django-free fastapp
  function. Physics already lives in `agri.core.agronomy.compute_et0_for_zone`; this iterates zones
  and upserts `Et0Calculated`/`VPDWeather` (one row per (zone, timestamp), idempotent). Additive
  until the F10 worker cutover.

2 golden parity tests: identical rows + return dict vs the Django task, idempotency, no-weather
  no-op. Full suite **317 passed**.

Remaining F8b: `flag_idle_zones`, `send_periodic_notifications`, `scan_device_health`,
  `scan_proactive_insights`, `run_due_irrigation_programs` → then F10 native worker.


## v1.97.0 (2026-07-04)

### Features

- **fastapp**: Port the 7 comms Celery task bodies (F8a)
  ([#339](https://github.com/AgriLogy/agri-api/pull/339),
  [`f6147fd`](https://github.com/AgriLogy/agri-api/commit/f6147fd2a78f6f526458cac3c515e088cdbfb7ab))

Closes #338

Strangler **F8a** — the seven on-demand notification-delivery tasks move to Django-free fastapp
  functions. **Additive**: the Django worker still executes everything; nothing in prod changes
  until the F10 worker cutover.

## Ported → `fastapp/tasks_comms.py` `send_alert_email` · `send_alert_digest_email` ·
  `send_alert_whatsapp` · `send_alert_sms` · `send_zone_outbound_{email,sms,whatsapp}`

## Building blocks - `fastapp/sms.py` — Twilio (copy of the already-Django-free
  `twilio_messaging.py`) - labels/units from `agri.core.alerts.SENSOR_KEY_REGISTRY`; email via
  `fastapp.email`; delivery-log via SQLAlchemy `AnalyticsNotificationdeliverylog`

## Parity 10 golden tests — Django task vs fastapp twin on the same rows, email/SMS captured both
  sides → identical return dict + delivery-log rows + composed message. Full suite **315 passed**,
  ruff + format clean.

Next: **F8b** (compute/scan tasks) → **F10** (native `fastapp/celery_app.py` worker + static beat).


## v1.96.1 (2026-07-04)

### Bug Fixes

- **fastapp**: Deterministic /users list tie-order (equal date_joined)
  ([#337](https://github.com/AgriLogy/agri-api/pull/337),
  [`70643ac`](https://github.com/AgriLogy/agri-api/commit/70643acaf585903b580efeb226b2423d13c038c8))

Closes #336

Live A/B on prod caught the last `/users` byte-parity gap: 3 prod users share an identical
  `date_joined`, and the list came out in a different order on each surface (undefined secondary
  sort). Added a `-id` tie-break to **both** the Django `list_users` and the fastapp query.
  Regression test with tied timestamps included; `/users` parity suite 25 passed.


## v1.96.0 (2026-07-04)

### Features

- **fastapp**: Port /auth login + token issuance to the sidecar (F9)
  ([#335](https://github.com/AgriLogy/agri-api/pull/335),
  [`c379afa`](https://github.com/AgriLogy/agri-api/commit/c379afa5af9775cbede92d9ea661204c3e7dcf53))

Closes #334

Strangler **F9** — the riskiest phase (login path for every user). Django `/auth` stays the live
  surface; **nginx is NOT flipped here** — the orchestrator flips it last with careful live A/B.

## Ported | Endpoint | Notes | |---|---| | `POST /auth/signup` | + `post_save(User)` bootstrap
  (per-user GraphName + SensorColor, Django defaults replicated) | | `POST /auth/sessions` | JWT
  pair; Django `login()` session cookie dropped (frontends are Bearer + SSO-localStorage only) | |
  `POST /auth/admin-sessions` | sign-in, `is_staff` omitted | | `DELETE /auth/sessions` | log out
  everywhere (bump `sessions_revoked_at`) | | `POST /auth/token/` · `POST /auth/token/refresh/` |
  legacy DRF simplejwt + revocation-aware refresh |

## Building blocks - `fastapp/tokens.py` — simplejwt-compatible mint (access 5d / refresh 10d).
  **Cross-mint proven**: a fastapp token validates under Django `AccessToken()`. -
  `passwords.verify_password` — pbkdf2_sha256, `check_password`-compatible (iteration count read
  from the hash). - In-process lockout (5/5min) mirroring Django's LocMem cache.

## Parity insight ninja `/auth` endpoints render **spaced** JSON; DRF `/auth/token/*` render
  **compact** — the latter use Starlette's stock JSONResponse (byte-compatible with DRF's renderer).

## Tests 16 golden parity tests — error/status envelopes byte-identical, token responses by decoded
  claims + cross-mint, signup asserts the same DB rows on both surfaces. Full fastapp suite **280
  passed**, ruff + format clean.


## v1.95.0 (2026-07-04)

### Features

- **fastapp**: Port /users admin console + /users/me/notifications (F5c)
  ([#333](https://github.com/AgriLogy/agri-api/pull/333),
  [`ce9ea75`](https://github.com/AgriLogy/agri-api/commit/ce9ea752b9ba60ecbe93aa3a61c2f45b4865b328))

Closes #332

Strangler F5c — the last user-facing Django ninja surface moves to the FastAPI sidecar.

## Ported (`apps/users/router_admin.py` → `fastapp/routers/users.py`) - `GET/POST /users`
  (list+search / create) - `GET/PATCH/DELETE /users/{username}` - `POST
  /users/{username}/{activate,password-reset,force-logout}` - `GET /users/{username}/sessions` -
  `POST /users/me/notifications`

## Route ownership (no collisions) - `GET/PATCH /users/me` stay with `selfreads.py` (byte-verified
  in F5). - `/users/{username}/{zones,alerts,activity,sensor-units}` stay with `admin_analytics.py`.
  - `users.router` is included AFTER both so the exact/existing routes keep their owners.

## Parity 24 byte-parity golden tests (Django ninja `APIClient` vs fastapp `TestClient`, same rows +
  token): reads / 404 / 403 / 400 byte-identical; mutations structural. Full fastapp suite: **288
  passed**, ruff + format clean.

## nginx (orchestrator to apply) After merge the whole `/users` prefix is portable to `:8001` —
  every `/users/*` route is now served by fastapp (selfreads + admin_analytics + this).


## v1.94.1 (2026-07-04)

### Bug Fixes

- **fastapp**: Close two admin-rest byte-parity gaps before cutover
  ([#331](https://github.com/AgriLogy/agri-api/pull/331),
  [`33123dc`](https://github.com/AgriLogy/agri-api/commit/33123dc93f50012b27c5983a99f00969a9412a2a))

Closes #330

Live A/B against prod on the F6-admin-rest routes (#329) surfaced two residual byte-parity
  divergences; this closes both so the routes can be flipped in nginx.

## 1. `/admin/alert-analytics` → `recently_triggered` tie order Alerts sharing an identical
  `last_triggered_at` came out in a different order between the Django ORM and the SQLAlchemy port
  (undefined secondary sort). Added a deterministic `-id` tie-break on **both** surfaces.

## 2. Validation 422 envelope The fastapp `RequestValidationError` handler emitted pydantic v2's
  `input`/`url`/`ctx` keys; django-ninja omits them. The handler now strips them → byte-identical
  422 on `/admin/sensor-data` and any required-param route.

## Not flipped `= /admin/monitoring` (exact) stays on Django — it returns an empty 404 there vs
  fastapp's JSON 404; only `/admin/monitoring/` sub-routes are byte-identical.

## Tests 262 fastapp suite green + 3 new parity tests (tie order, 422 envelope).


## v1.94.0 (2026-07-04)

### Features

- **fastapp**: Cut remaining admin routes over + fix /admin/monitoring byte-parity (F6-admin-rest)
  ([#329](https://github.com/AgriLogy/agri-api/pull/329),
  [`fd84633`](https://github.com/AgriLogy/agri-api/commit/fd84633b1de01d68111862da6a63cf49435ec41f))

Closes #328

Ports the last django-ninja admin routers to the `fastapp` strangler sidecar (SQLAlchemy via
  agri-core, no Django ORM) with wire byte-parity, and closes a byte-parity gap in the
  already-ported `/admin/monitoring`.

## Routers ported | Django source | fastapp router | paths | |---|---|---| |
  `apps/irrigation/router_admin.py` | `routers/admin_analytics.py` | `/admin/overview`,
  `/admin/analytics`, `/admin/devices/health`, `/admin/alerts` (+`{pk}`), `/admin/alert-analytics`,
  per-user
  `/users/{u}/zones|params|active-graph|graph-names|sensor-colors|alerts|activity|sensor-units` | |
  `apps/sensors/router_sensor_data.py` | `routers/admin_sensor_data.py` | `/admin/sensor-data`
  catalog + list + patch + delete + range-delete | | `apps/irrigation/router_backfill.py` |
  `routers/admin_backfill.py` | `/admin/users/{u}/zones/{z}/backfill[-status]` | |
  `apps/irrigation/router_impersonation.py` | `routers/admin_impersonation.py` |
  `/admin/impersonate/{username}` |

All require JWT + `is_staff` (`get_current_staff_user`). Byte-parity matches `model_to_dict` field
  order, `.isoformat()` timestamps, Decimal→float, and the 404/400 envelopes. Timestamped writes
  (create/patch) and the minted impersonation token are inherently non-deterministic, so they're
  compared structurally (shape + decoded claims). The impersonation token is minted with PyJWT to
  carry the SAME simplejwt `AccessToken` claims (`token_type`/`user_id`/`jti`/`exp`/`iat` +
  `readonly`/`impersonator*`), verifiable against the shared `SECRET_KEY`.

## /admin/monitoring byte-parity fix **Root cause:** a DatabaseScheduler `SolarSchedule` row
  rendered its schedule string from the raw `event` value (`sunrise (…)`), whereas
  django_celery_beat's `SolarSchedule.__str__` uses `get_event_display()` — the human label
  (`Sunrise (…)`). Semantically equal, one byte different. **Fix:** map the event through the
  `SOLAR_SCHEDULES` label table in `admin_monitoring._database_beat_schedule`. `overview` + `tasks`
  are now `dj.content == fp.content` even with a solar schedule present (regression test added).

## nginx (deploy/nginx/back.conf) Cut over the cleanly-owned prefixes to `:8001`: `=
  /admin/overview`, `= /admin/analytics`, `= /admin/alert-analytics`, `= /admin/alerts` +
  `/admin/alerts/`, `/admin/devices/`, `= /admin/sensor-data` + `/admin/sensor-data/`,
  `/admin/users/` (backfill), `= /admin/impersonate` + `/admin/impersonate/`, and now `=
  /admin/monitoring` (the `/admin/monitoring/` prefix was already cut over). The analytics router's
  `/users/{username}/*` paths are **NOT** cut over — they share the `/users` prefix with the
  still-Django users-admin router, so they stay on Django; the ports are exercised via the sidecar
  TestClient.

## Tests New `src/fastapp/tests/test_adminrest_parity.py` (42 tests): non-staff 403s, byte-parity
  reads + 404/400 envelopes, structural write checks, backfill count parity, impersonation claim
  decode, and the monitoring solar byte-parity assertion for **both** `overview` and `tasks`. Full
  fastapp suite green (234 passed) on Postgres. Ruff clean.

## Out of scope `apps/users/router_admin.py` (the `/users` admin tree) is intentionally left on
  Django. `/admin/db` (another agent's generic CRUD) is untouched.


## v1.93.0 (2026-07-04)

### Features

- **fastapp**: Cut /admin/db generic CRUD over to the sidecar (F6-admin-db)
  ([#327](https://github.com/AgriLogy/agri-api/pull/327),
  [`e991937`](https://github.com/AgriLogy/agri-api/commit/e9919370e0a949a99455a7a29bf91888bb75d0b9))

Closes #326

Ports the generic, schema-driven database back-office (`/api/admin/db/*`) from the django-ninja
  router (`agriapi/api/router_db.py`) to the fastapp sidecar. This is the hardest strangler port:
  the Django version introspects **Django models**; the sidecar re-implements it over the **agri.db
  SQLAlchemy metadata** (`AgriBase.registry.mappers` + `sqlalchemy.inspect`), with **no Django
  ORM**.

## Routes (staff-only, mounted at the URL root) | method | path | purpose | |---|---|---| | GET |
  /admin/db/tables | list every model + row count | | GET | /admin/db/tables/{key}/schema | field
  schema for one model | | GET | /admin/db/tables/{key}/rows | paginated / searchable list | | POST
  | /admin/db/tables/{key}/rows | create a row | | GET | /admin/db/tables/{key}/rows/{pk} | retrieve
  one row | | PATCH | /admin/db/tables/{key}/rows/{pk} | update a row | | DELETE |
  /admin/db/tables/{key}/rows/{pk} | delete a row |

## Table-key mapping (kept byte-identical to Django's `label_lower`) `key =
  f"{app_label}.{model_name}"` derived from `__tablename__` — only `model_name` is lowercased
  (`app_label` keeps its case, e.g. **`CustomUser.customuser`**). Default `{app}_{model}` tablenames
  split on the first `_`; two special cases: - `assistant_conversation` →
  **`assistant.assistantconversation`** (custom db_table; explicit override). - the auto-created M2M
  through tables (`CustomUser_customuser_{groups,user_permissions}`) are **hidden** — Django's
  `get_models()` excludes them.

## Table-set delta vs Django (documented + asserted in the test) - **fastapp-only:**
  `analytics.devicesensor` — exists in the agri.db schema-of-record; the Django DeviceSensor model
  isn't in this repo yet (so the sidecar can already manage it). - **Django-only:** `auth.group`,
  `auth.permission`, and the six `django_celery_beat.*` tables — Django-runtime apps not mirrored in
  agri.db. - All other **68 keys are shared** with byte-identical db_table.

## What byte-matches, and what can't Full byte-parity on `/tables` + `/schema` is **not**
  attainable: Django's `verbose_name`, `help_text`, `choices`, per-field `required`/`editable` and
  the field ordering all come from Django model metadata absent from the DB/SQLAlchemy layer. What
  IS byte-identical (and what the frontend keys off): the table **key** format, `app_label`,
  `model_name`, `pk_field`, the set of field names + each field's `type`/`primary_key`/`nullable` +
  the FK `relation.to` target, the row-CRUD JSON coercion (dates → ISO, Decimal → float, bytes →
  None), and the `{"detail": ...}` error envelopes.

Two field-level deltas are pinned in the test where the SA mirror (source = the live DB) diverges
  from Django's declared model: - `assistant.assistantconversation.user_id` — SA mirror has no FK
  constraint (assistant tables were absorbed from ensure_* boot scripts without it) → `integer` vs
  Django's `fk`. - `feedback.bugreport.video_url` — `nullable=True` in the real DB column, but the
  Django model declares `null=False` (fastapp reflects the actual schema).

## Tests `src/fastapp/tests/test_admindb_parity.py` (20 cases): non-staff 403 / unauth 401, the
  table-key subset + set-delta, schema derivable-projection parity vs Django's own `_schema` (+ the
  two pinned deltas), the row-list envelope + search + pagination, the full CRUD lifecycle, and
  byte-identical 404 error envelopes. Full fastapp suite: **212 passed**. Ruff clean.

nginx: added `= /admin/db` + `/admin/db/` → :8001 in `deploy/nginx/back.conf`.


## v1.92.0 (2026-07-04)

### Features

- **fastapp**: Cut /ingest device webhooks over to the sidecar (F9-ingest)
  ([#325](https://github.com/AgriLogy/agri-api/pull/325),
  [`22d8539`](https://github.com/AgriLogy/agri-api/commit/22d8539ca561efe7c2229020995a8815a724759a))

Closes #324

Strangler phase **F9-ingest**: ports the device-ingest webhooks from django-ninja to the FastAPI
  sidecar (`fastapp`), byte-parity verified.

## Routes ported (all `auth=None` — device shared-secret, not JWT) | fastapp route | Django source |
  behaviour | |---|---|---| | `POST /ingest/bivocom` | `apps/bivocom/router.py` | stub (validate +
  202, no persist) — matches the current ninja stub | | `POST /ingest/lorawan/chirpstack` |
  `apps/lorawan/chirpstack/router.py` | decode pH/battery/rssi → append `lora_uplink` + per-metric
  rows under the `lora` zone, dispatch alerts | | `POST /ingest/weather` |
  `apps/sensors/router_weather_ingest.py` | registry-driven multi-sensor ingest + alert dispatch | |
  `POST /ingest/sensor` | `apps/sensors/router_weather_ingest.py` | single typed reading + alert
  dispatch |

## How - Readings persist through the shared **agri-core SQLAlchemy** session
  (`session_scope(commit=True)`) — no Django ORM. - **Device decode** (ChirpStack RS485-LB
  pH/battery) is pure Python and was replicated verbatim in `fastapp/ingest.py` (the Django router
  held it inline; agri-core only owns the pure alert evaluator, not the decode). Bivocom has no
  adapter yet on either side. - **Alert dispatch** (`dispatch_alerts_for_reading`) is a SQLAlchemy
  port of the Django `apps/alerts/engine.py` adapter: same alert-matching (farm-zone + user-wide +
  custom notification-zone #57), same **atomic grace claim** (conditional `UPDATE ...
  last_emailed_at`), same per-channel + digest Celery enqueue via `fastapp.celery.send_task` (same
  task names + kwargs the Django `.delay(...)` used). Grace table mirrors `settings/base.py`. -
  `lora_uplink` isn't modelled in agri.db (Django owns it, managed=False), so a minimal SQLAlchemy
  model on a private Base maps it for INSERT — kept out of `AgriBase.metadata` so Alembic
  autogenerate is untouched.

## Parity test — `fastapp/tests/test_ingest_parity.py` (dual-ORM, Postgres) 8 tests, all green.
  Drives BOTH surfaces over one DB and asserts identical status + **byte-identical** response
  bodies, the **same rows written** (LoraUplink + per-metric + weather), and the **same alert task
  enqueued with the same kwargs** (Django `.delay` + `send_task` both monkeypatched to no-ops — no
  broker). Full fastapp suite: **200 passed**.

## nginx Adds `location /ingest/` → `127.0.0.1:8001` in `deploy/nginx/back.conf` (single prefix
  covers bivocom/chirpstack/weather/sensor). Manual apply on the droplet as usual.

## Not byte-matched (by design) Malformed-payload **422** envelopes differ between ninja and FastAPI
  validation (inherent framework difference) — parity is asserted on the valid path + the
  hand-rolled 400/200/202 envelopes, which ARE byte-identical.


## v1.91.0 (2026-07-03)

### Features

- **fastapp**: Cut business-admin routes over to the sidecar (F6-business-admin)
  ([#323](https://github.com/AgriLogy/agri-api/pull/323),
  [`a6799b1`](https://github.com/AgriLogy/agri-api/commit/a6799b1ef830354164bb5da12e00974ff481653b))

Closes #322

Strangler phase **F6**: ports the staff-only business-admin django-ninja routers to the FastAPI
  sidecar (`fastapp`, :8001) with **byte-identical** parity, and adds the matching nginx location
  blocks so each prefix strangles over to :8001.

## Routes ported (23) | prefix | routes | |---|---| | `/admin/billing` | GET/POST `/plans`,
  PUT/DELETE `/plans/{id}`, GET/POST `/subscriptions`, POST `/subscriptions/{id}/cancel`, GET/POST
  `/invoices`, POST `/invoices/{id}/mark-paid` | | `/admin/audit` | GET `/admin/audit` (actor /
  action-icontains / target_type / limit filters) | | `/admin/settings` | GET (seeds defaults),
  PATCH (upsert), POST (create, 409 on dup), DELETE `/{key}` | | `/admin/kc` | GET/POST list+create,
  GET/PUT/DELETE `/{id}` (cross-user, adds `username`) | | `/admin/monitoring` | GET `/overview`,
  `/tasks` (+ beat schedule), `/deliveries`, `/logins` | | `/admin` records | GET/DELETE
  `/notifications`, GET/DELETE `/conversations`, GET `/proactive-notices` + DELETE reset,
  GET/PATCH/DELETE `/technician-grants` |

## How - All routes staff-gated via `get_current_staff_user` (401 bad token, 403 non-staff). -
  Reads/writes go through the **agri-core SQLAlchemy** session — no Django ORM. Unmanaged tables
  (`lora_uplink`, `django_celery_beat_*`) read via raw SQL. - Writes replicate Django's app-layer
  defaults (`created_at`/`issued_at`/`updated_at`) and mirror `payement_status` onto the user; admin
  mutations still record an `analytics_auditevent` row. - The monitoring **beat schedule** is
  reproduced without importing Django: the static `CELERY_BEAT_SCHEDULE` is rebuilt from the same
  env-driven `celery.schedules.crontab` logic, and `django_celery_beat` DatabaseScheduler rows are
  read via raw SQL with each schedule's `__str__` reconstructed exactly.

## Not in scope (stay on Django) `/admin/db` (generic CRUD), `/admin/overview`, `/admin/analytics`,
  `/admin/alerts`, `/admin/devices`, `/admin/sensor-data`, `/admin/backfill`, `/admin/impersonate`,
  `/admin/users/*`. The nginx blocks are scoped to exactly the ported prefixes so these are
  untouched.

## Tests `fastapp/tests/test_adminbiz_parity.py` — dual-ORM Postgres golden parity: same committed
  rows + same Django-minted staff token, asserting `dj.content == fp.content` for reads and
  shape+refetch for writes, plus a 403 check per route and byte-parity of the DB-backed beat
  schedule. **38 passed**; full fastapp suite **140 passed**; ruff clean.

## Known non-byte-match (documented) - **Non-staff 403 body**: Django `_require_admin` returns
  `{"detail": "Admin access required"}`; the sidecar's `get_current_staff_user` returns `{"detail":
  "You do not have permission to perform this action."}`. Only the 403 **status** is asserted (per
  plan). Authenticated 401 envelopes already match. - **Billing subscription with
  `period_start`/`period_end`**: Django assigns the raw string to a `DateField` and then calls
  `.isoformat()`, which 500s; the port correctly parses the date instead. The realistic admin path
  (dates omitted) is byte-identical.


## v1.90.0 (2026-07-03)

### Features

- **fastapp**: Cut /assistant over to the sidecar (F7)
  ([#321](https://github.com/AgriLogy/agri-api/pull/321),
  [`a17ead0`](https://github.com/AgriLogy/agri-api/commit/a17ead0bea7384868043796e98fd0346f4d33b1b))

Closes #320

Strangler phase **F7**: ports the django-ninja `apps/assistant/router.py` to the FastAPI sidecar
  (`fastapp`). 10 groups already live on the sidecar; this adds `/assistant`.

## Routes ported (mounted in `fastapp/main.py`) | Method | Path | |---|---| | GET |
  `/assistant/tools` — tool catalog | | POST | `/assistant/tools/{name}` — invoke one tool | | POST
  | `/assistant/chat` — orchestrated message → intent/reply/tool/data | | GET |
  `/assistant/conversations` | | PUT | `/assistant/conversations/{client_id}` | | DELETE |
  `/assistant/conversations/{client_id}` |

## How it's built - New `fastapp/assistant/` package mirroring the Django one: `registry.py`
  (sensor-key → agri-db model + label/unit, byte-identical keys/labels/order), `tools.py` (14-tool
  catalog + handlers), `orchestrator.py` (rule-based, verbatim rule table), `llm.py`
  (OpenAI-compatible tool-caller over stdlib urllib). - **All DB access is SQLAlchemy via
  agri-core** (no Django ORM). Reads open a session; the two mutating tools (`create_alert`,
  `set_notification_cadence`) use `session_scope(commit=True)`. Conversations live in
  `assistant_conversation` (agri-db `AssistantConversation`). - Irrigation advice Tier-1 reuses
  agri-core's `field_snapshot_for_user` (the SQLAlchemy twin of the Django
  `agriapi.agronomy.field_snapshot` adapter); Tier-2 fallback is pure math, identical to Django. -
  Added `AI_API_KEY` / `AI_API_BASE_URL` / `AI_MODEL` / `AI_TIMEOUT` to `AppSettings` (same env vars
  Django reads; no new names). Empty key ⇒ rule-based orchestrator, exactly like Django.

## Parity `src/fastapp/tests/test_assistant_parity.py` — dual-ORM golden parity on Postgres (18
  tests, all green): - **byte-identical**: tool catalog, unknown-tool 404, conversation CRUD
  (list/upsert/replace/delete + user isolation), `/chat /sitemap`, `/chat` smalltalk,
  technician-blocked `create_alert`. - **JSON parity** (ids match by construction):
  `get_active_alerts`, `get_farm_status`, `list_recent_notifications`, `/chat /alerts`, `/chat
  /help`. - **`create_alert`** writes: response shape + row match (ids differ). - **`/chat` LLM
  path**: the model reply is non-deterministic in prod, so it's **not** byte-matched — the LLM
  `_post` is mocked identically on BOTH sides to assert the envelope + tool-routing + tool data
  match. With no key, `/chat` is byte-identical.

Full fastapp suite: 120 passed. Ruff clean.

## nginx (HELD — do not merge with app) Added `location = /assistant` + `location /assistant/` →
  `:8001` in `deploy/nginx/back.conf`, matching the existing cutover blocks. Not activated until
  deploy.


## v1.89.0 (2026-07-03)

### Features

- **fastapp**: Cut /devices + /technicians + /irrigation over to the sidecar (F5b)
  ([#319](https://github.com/AgriLogy/agri-api/pull/319),
  [`5a18964`](https://github.com/AgriLogy/agri-api/commit/5a18964d1c7cd6912eff6d37db33608f544140a0))

Closes #318

Strangler phase **F5b**: three more django-ninja routers move to the FastAPI sidecar (`:8001`),
  byte-parity preserved. nginx (`deploy/nginx/back.conf`) location blocks added for each prefix so
  prod routes cut over on deploy.

## Routers ported | Django source | Sidecar | Access | |---|---|---| |
  `apps/irrigation/router_devices.py` | `fastapp/routers/devices.py` — `/devices` | admin
  (`is_staff`) | | `apps/users/router_technicians.py` | `fastapp/routers/technicians.py` —
  `/technicians` | owner (non-technician) | | `apps/irrigation/router_irrigation_automation.py` |
  `fastapp/routers/irrigation.py` — `/irrigation` | caller-scoped; technician read-only |

## Notes - **No agri.db ORM models** exist for `analytics_device`, `analytics_irrigationprogram`,
  `analytics_outputcommand`, `analytics_techniciangrant`, `analytics_technicianzonegrant` (all
  Django-managed / unmirrored), so every read/write is parameterised raw SQL over the agri-core
  session — the `selfreads.py` pattern. - **Technician passwords** are hashed with a new Django-free
  `fastapp/passwords.py` (stdlib `pbkdf2_hmac`, `pbkdf2_sha256$600000$`, Django-format salt). The
  stored hash verifies against Django's `check_password` (asserted in the test). Password-validation
  error messages (min-length / common / numeric) are reproduced byte-for-byte, reading Django's
  bundled common-password list directly. - **Irrigation dispatch** simulation path inlined (mirrors
  `output_dispatch.dispatch_command`); new `IRRIGATION_DISPATCH_ENABLED` mirror added to
  `fastapp/settings.py`. - Error envelopes, field order, and `isoformat`/time rendering all matched.

## Tests `fastapp/tests/test_devices_parity.py` — 34 dual-ORM golden-parity cases (Postgres). Full
  sidecar suite green: **136 passed**.


## v1.88.0 (2026-07-03)

### Features

- **fastapp**: Cut /users/me + /zones self-reads over to the sidecar (F5-reads)
  ([#313](https://github.com/AgriLogy/agri-api/pull/313),
  [`4a2a01c`](https://github.com/AgriLogy/agri-api/commit/4a2a01c867414463b7c15b473aaaa97392cbb763))

Closes #312

## What

Ports the **F5-reads** self-scoped group from django-ninja to the `fastapp` FastAPI sidecar,
  byte-parity with the Django originals:

- **GET/PATCH `/users/me`** — caller's profile (`{username, preferred_language, notify_every}`);
  PATCH self-updates `preferred_language`. Invalid language returns the bare field map
  `{"preferred_language": "Must be 'fr' or 'ar'."}` @ 400 (not the `{detail}` envelope), matching
  ninja. - **GET `/zones`** — caller's zones (`[{id, name}]`). - **GET `/zones/{id}/active-graph`**
  — `model_to_dict(ActiveGraph)` minus `id`: FK ids under `user`/`zone`, then every `*_status`
  boolean in the Django model's field-declaration order; 404 `{"detail": "ActiveGraph not found."}`
  on miss/foreign zone.

## How

- Data access is SQLAlchemy via agri-core's `session_scope` (no Django ORM). Owner-scoped by
  `user.id`; PATCH uses `commit=True`. - Ports `resolve_read_scope` so **technician** (scoped
  read-only) callers keep parity too — regular users get an unrestricted own-id scope; technicians
  get the owner's rows narrowed to granted zones + per-zone graph whitelist. The unmanaged grant
  tables (no agri.db ORM model) are read with parameterised raw SQL. - Response bytes go through
  `DjangoStyleJSONResponse` (spaced separators, ascii) so a cutover is byte-identical, not just
  parse-identical.

## nginx cutover

Added to `deploy/nginx/back.conf` (above the catch-all), capturing ONLY the ported routes: -
  `location = /users/me` (exact) — leaves the still-Django `/users/me/notifications` (POST) and the
  `/users` admin tree (`/users`, `/users/<username>/*`) on :8000. - `location = /zones` + `location
  /zones/` — together capture exactly the two ported routes; the distinct `/notification-zones`
  prefix is untouched.

## Tests

New dual-ORM Postgres parity suite `fastapp/tests/test_selfreads_parity.py` (10 tests) drives both
  surfaces with the same token + data and asserts `status_code` + byte-identical `content` for:
  /users/me GET, PATCH (valid / no-op / invalid-400), /zones (populated / empty / no owner leak),
  active-graph (ok / missing-404 / foreign-zone-404), and 401. All green (also re-ran the weather
  suite — no regression). Ruff clean.


## v1.87.0 (2026-07-03)

### Features

- **fastapp**: Cut /notifications + /notification-zones over to the sidecar (F3-notifications)
  ([#315](https://github.com/AgriLogy/agri-api/pull/315),
  [`c34f3ef`](https://github.com/AgriLogy/agri-api/commit/c34f3efca30c6793ce2bde68ff4fe7123dc2d518))

Closes #314

Strangler phase **F3**: moves the notifications group from django-ninja to the fastapp sidecar
  (`:8001`), byte-for-byte, following the F2 (weather), F2b (sensors) and F2c (feedback) cutovers.

## Routes ported | Method | Path | Notes | |---|---|---| | GET | `/notifications` | feed — 200 most
  recent, newest first | | POST | `/notifications/zone-outbound` | email / SMS / WhatsApp; Celery
  enqueue | | GET, POST | `/notification-zones` | list / create | | GET |
  `/notification-zones/available-sensors` | registered before `/{pk}` | | GET, PATCH, DELETE |
  `/notification-zones/{pk}` | owner-scoped | | POST | `/notification-zones/{pk}/sensors` | add
  assignment | | DELETE | `/notification-zones/{pk}/sensors/{sensor_id}` | remove assignment |

## How - **SQLAlchemy via agri-core** (no Django ORM). Reads use `session_scope()`; writes use
  `session_scope(commit=True)` with explicit `created_at`/`updated_at` to mirror the Django model's
  `auto_now_add` / `auto_now` columns. - **Celery** enqueue via a new `fastapp/celery.py`
  `send_task` helper — same task names (`agriapi.tasks.send_zone_outbound_{email,sms,whatsapp}`) and
  kwargs the ninja route used with `.delay(...)`; no import of `agriapi.tasks`. - **Technician**
  writes are blocked (403) by resolving `is_technician` from the user row (kept off `AuthedUser` so
  the F1 auth-parity contract is unchanged). - **available-sensors** resolves each registry key to
  its agri-db model via `agri.core.alerts.db_model_for` (the SQLAlchemy analogue of Django's
  `get_sensor_model`). - **nginx**: added `= /notifications` + `/notifications/` and `=
  /notification-zones` + `/notification-zones/` location blocks → `:8001`.

## Byte-parity - `DjangoStyleJSONResponse` for direct bodies; `raise HTTPException(N, ...)` for
  `{"detail": ...}` envelopes; spaced-separator ASCII JSON throughout. - New dual-ORM parity suite
  `fastapp/tests/test_notifications_parity.py` drives **both** surfaces over the same committed rows
  + the same Django-minted token and asserts identical status + bytes for reads/errors, identical
  shape for writes, and the 202/400/401 contract for the Celery route.

## Tests - `test_notifications_parity.py`: **25 passed**. - Full fastapp suite: **59 passed**. Ruff
  check + format clean.

## Deploy note Cutover is inert until the nginx blocks are reloaded on the droplet; rollback =
  delete the blocks + `nginx -t && systemctl reload nginx`.


## v1.86.0 (2026-07-03)

### Features

- **fastapp**: Cut /alerts + /kc + /manager-affirmations over to the sidecar (F3-alerts)
  ([#317](https://github.com/AgriLogy/agri-api/pull/317),
  [`6870306`](https://github.com/AgriLogy/agri-api/commit/6870306fcacb2de04cc19ca164bdbb061f129e00))

Closes #316

Continues the strangler migration (F2 weather/feedback/sensors already on the sidecar) by porting
  the **F3-alerts** router group to the FastAPI sidecar (`fastapp`), **byte-identical** to the
  django-ninja originals. Each cut-over prefix gets a matching nginx `location` block and a golden
  parity test that drives BOTH surfaces over the same committed Postgres rows + the same
  Django-minted token and asserts `dj.content == fp.content`.

## Routers ported | ninja source | sidecar | routes | |---|---|---| | `apps/alerts/router_alerts.py`
  | `fastapp/routers/alerts.py` | `GET/POST /alerts`, `GET /alerts/for-graph`, `GET
  /alerts/suggest`, `GET/PUT/PATCH/DELETE /alerts/{pk}` | | `apps/irrigation/router_kc.py` |
  `fastapp/routers/kc.py` | `GET/POST /kc`, `GET/PUT/DELETE /kc/{kc_id}` | |
  `apps/irrigation/router_manager_affirmation.py` | `fastapp/routers/manager_affirmations.py` |
  `GET/POST /manager-affirmations`, `POST /manager-affirmations/{pk}/approve|reject` |

## Notes - All data access is SQLAlchemy via agri-core (`session_scope`); no Django ORM. The alert
  fan-out (`recent_triggers_for_user`) and suggestion (`suggest_alert_for`) already live in
  `agri.core.alerts` and are called directly. - The `manager-affirmations` approve path ports the
  Django `affirmation_appliers` (zone-params / kc-periods / user-reactivate) to SQLAlchemy so the
  whole prefix can cut over. - `AuthedUser` gains an `is_technician` flag (real `CustomUser` column)
  to reproduce the technician read-only 403. - Two byte-parity subtleties handled: Django's
  `model_to_dict` field order (incl. the computed `threshold`) and `condition_nbr` rendered as its
  Decimal string `"30.00"` — the alert reads return `DjangoStyleJSONResponse` directly so FastAPI's
  `jsonable_encoder` can't coerce the Decimal to a float. - nginx: added `location = /alerts` +
  `/alerts/`, `/kc` + `/kc/`, `/manager-affirmations` + `/manager-affirmations/` → `:8001`.

## Tests `67 passed` — the three new golden parity suites (`test_alerts_parity.py`,
  `test_kc_parity.py`, `test_manager_affirmations_parity.py`) plus the existing sidecar suite
  (updated `test_auth_parity.py` for the new `is_technician` field). Dual-ORM: Postgres-only,
  committed rows.


## v1.85.0 (2026-07-03)

### Features

- **fastapp**: Cut /sensors over to the FastAPI sidecar (F2b)
  ([#311](https://github.com/AgriLogy/agri-api/pull/311),
  [`303d5d9`](https://github.com/AgriLogy/agri-api/commit/303d5d962ecd62d7f9d4d0dd6f19cbc4ec8bca2c))

Closes 310

The highest-traffic cutover — every chart hits `/sensors/<slug>`.

- `fastapp/sensors.py`: `SENSOR_SPEC` (37 sensors — units + field order extracted from the Django
  models for byte-parity) + `hourly_readings` / `raw_readings` / `serialize_raw`. Aggregation
  delegated to agri-core `AgriMainDBClient` (same as the Django engine); no Django ORM. -
  `fastapp/routers/sensors.py`: GET `/sensors` (catalog), GET `/sensors/{slug}` (`?raw=true`), PATCH
  `/sensors/{slug}`. Same key order + unit metadata; same 404 shapes (`{detail}` for unknown slug,
  bare `{error: Not found}` for a missing row). - `fastapp/tests/test_sensors_parity.py`: **golden
  parity across ALL 37 slugs × (aggregated + raw)** + catalog + PATCH + both 404s + empty-list —
  byte-identical. 34 fastapp tests pass on Postgres 18. - `deploy/nginx/back.conf`: `location =
  /sensors` + `location /sensors/`.

Prereqs met: agri-db 0.14.0 (#307). After merge+deploy, apply the /sensors nginx blocks (manual,
  alongside /weather + /feedback).


## v1.84.0 (2026-07-03)

### Features

- **fastapp**: Cut /feedback over to the FastAPI sidecar (F2c)
  ([#309](https://github.com/AgriLogy/agri-api/pull/309),
  [`c965b38`](https://github.com/AgriLogy/agri-api/commit/c965b383a241bb560d4e287101e8ff9ea2a660b6))

Closes 308

Second strangler cutover — **POST /feedback** on the :8001 sidecar.

- `fastapp/routers/feedback.py`: writes FeedbackBugreport via agri-core SQLAlchemy (no Django ORM);
  `fastapp/email.py` stdlib Resend client for the best-effort internal-team email. Same validation,
  metadata→column promotion, 201 {id,status} + 400 envelope as ninja. -
  `fastapp/tests/test_feedback_parity.py`: both surfaces over the same committed data/token →
  identical column writes + byte-identical 400. 28 fastapp tests pass on PG. -
  `deploy/nginx/back.conf`: `location = /feedback → :8001`.

⚠️ **Deploy order**: needs the deployed image at agri-db 0.14.0 (#307) — merging + deploying while
  the image is still 0.11.1 crashloops the sidecar. Verifying the image before merge.


## v1.83.0 (2026-07-03)

### Features

- **deps**: Bump agri-core 0.18.1 -> 0.19.0 (agri-db 0.14.0)
  ([#307](https://github.com/AgriLogy/agri-api/pull/307),
  [`4783b41`](https://github.com/AgriLogy/agri-api/commit/4783b417ac0882e81acfe20fa7e3c1fe6d28124f))

Closes 306

Transitively upgrades agri-db 0.11.1 → 0.14.0, closing the api→core→db pin gap. Picks up
  FeedbackBugreport (unblocks fastapp /feedback), the absorbed ensure-script tables, and
  CustomUser.is_technician. Full suite: 564 passed locally; the only failures were
  Celery/Redis-dependent notification tests (no local Redis — green in CI).


## v1.82.1 (2026-07-03)

### Bug Fixes

- **fastapp**: Django-ninja-style JSON for byte-identical cutover parity
  ([#305](https://github.com/AgriLogy/agri-api/pull/305),
  [`fd60eb6`](https://github.com/AgriLogy/agri-api/commit/fd60eb65830d4170bd36b607886279e78dcd0294))

Closes 304

Follow-up to #303. `DjangoStyleJSONResponse` (spaced separators + ascii) as the app default response
  class + the HTTPException/validation error handlers, so every cut-over route matches ninja's wire
  format byte-for-byte. Weather parity tests upgraded to assert `dj.content == fp.content`. 24
  fastapp tests pass on Postgres 18.


## v1.82.0 (2026-07-03)

### Features

- **fastapp**: Cut /weather over to the FastAPI sidecar (F2)
  ([#303](https://github.com/AgriLogy/agri-api/pull/303),
  [`dfeacac`](https://github.com/AgriLogy/agri-api/commit/dfeacac5e10ad50ecb3489ffaeb676c042eab3b3))

Closes 302

## What First strangler cutover — **GET /weather/et-forecast** now has a FastAPI implementation on
  the :8001 sidecar, byte-for-byte compatible with the django-ninja route.

- `fastapp/routers/weather.py`: owner-scoped zone + user lat/lon via agri-core SQLAlchemy (no Django
  ORM), the framework-agnostic `forecast_provider`, pure `agri.core.et0_forecast`. Same
  route/params/clamp/404-shape/response. - `fastapp/tests/test_weather_parity.py`: **golden parity**
  — every test drives BOTH the Django ninja endpoint (APIClient) and the fastapp route (TestClient)
  over the same committed data + same Django-minted token, asserting identical JSON incl. the 404
  envelope (`{"detail": "Zone not found."}`) and 401 on missing auth. 5 tests, verified locally on
  Postgres 18; full fastapp suite 24 passed. - `deploy/nginx/back.conf`: `location /weather/ ->
  127.0.0.1:8001` (most-specific-first).

## Cutover (manual, after merge+deploy) On the droplet: apply the /weather/ block + `nginx -t &&
  systemctl reload nginx`. Rollback = delete the block + reload (Django still serves /weather).
  Smoke: `curl :8001/weather/et-forecast` (401 no-auth) + a real authed request.

## Deferred (own PRs) - **/feedback** — needs the agri-db pin bump (installed 0.11.1 lacks
  `FeedbackBugreport`, added in 0.13.0). - **/sensors** — highest-traffic + hardest (dynamic
  per-model reading serialization + a PATCH write); careful separate port.


## v1.81.0 (2026-07-02)

### Continuous Integration

- Make the droplet deploy manually dispatchable
  ([#301](https://github.com/AgriLogy/agri-api/pull/301),
  [`bd53723`](https://github.com/AgriLogy/agri-api/commit/bd53723ed98f269272a7c156a6b92155e0f7bb62))

Closes 300

One-line trigger addition: `workflow_dispatch: {}` on deploy-back.yml. Also serves as the empirical
  test that push-event runs fire again after the #299 workflow swap (its merge SHA produced zero
  runs).

### Features

- **feedback**: Post /feedback stores in-app bug reports + emails internal team
  ([#293](https://github.com/AgriLogy/agri-api/pull/293),
  [`999e01d`](https://github.com/AgriLogy/agri-api/commit/999e01d32de5fb951c2795a08ed45eae2c66667a))

Closes #292

## What - New `apps/feedback` app: unmanaged `BugReport` model over `feedback_bugreport` +
  django-ninja router mounted at `/feedback`. - Reporter identity (id/email) derived from the JWT
  server-side — never trusted from the body. - Best-effort email to `INTERNAL_FEEDBACK_EMAILS` (new
  csv setting, defaults to the internal_* team addresses) through the existing Resend backend;
  failures never block the submission. - Model auto-appears in the generic `/api/admin/db`
  back-office CRUD (`feedback.bugreport`).

## Deploy order ⚠️ Merge + migrate **AgriLogy/agri-db#48** first (creates `feedback_bugreport`),
  then this.

## Verification - Django system check + ruff clean; smoke test: 201 + row create + email fan-out to
  the 3 internal recipients; 400 on empty description.


## v1.80.0 (2026-07-02)

### Continuous Integration

- Adopt AgriLogy/shared-workflows callers ([#299](https://github.com/AgriLogy/agri-api/pull/299),
  [`24d1c0b`](https://github.com/AgriLogy/agri-api/commit/24d1c0bc2fec7a0b6b94cdf494e2b424735b642b))

Closes #298

## What All five workflows become thin callers into `AgriLogy/shared-workflows@v1` (org access
  enabled); this repo keeps only triggers + repo-specific inputs. Independent of the fastapp stack
  (#295/#297) — branched off main.

| File | Now | Notes | | --- | --- | --- | | `primary.yml` (replaces `ci.yml`) |
  `python-lint.yml@v1` + `python-test.yml@v1` | lint → test chain; `working_directory: back`; test
  reproduces today's gate: Postgres **16** service, `django_check`, `coverage_min: 85`,
  `pytest_args: "src/ -v"`, identical env block; `secrets: inherit` forwards `AGRI_DB_RO_TOKEN`
  (private agri-db transitive dep) | | `release.yml` | `release.yml@v1` | `[skip ci]` head-commit
  guard, `concurrency: release-…`, push+`workflow_dispatch` (release_type stable/rc) all stay
  caller-side; `working_directory: back`, `prerelease: ${{ inputs.release_type == 'rc' }}` — same
  PSR version (v9.21.0), same pinned author | | `lint-pr-title.yml` | `lint-pr-title.yml@v1` | same
  trigger types + permissions; shared body is the identical amannn/action-semantic-pull-request
  config | | `auto-assign.yml` | `auto-assign.yml@v1` | same triggers; default assignee mks-zakaria
  | | `deploy-back.yml` | `deploy-droplet.yml@v1` | `repo_dir: /root/agri-api`, `branch: main`,
  `build_services: agri-api-web`, `migrate_service: agri-api-web` + `migrate_command: bash
  /code/docker-entrypoint.sh migrate`, `compose_services: "agri-api-web agri-api-worker
  agri-api-beat"`; `secrets: inherit` forwards DO_HOST/DO_USER/DO_SSH_KEY/AGRI_DB_RO_TOKEN |

Also adds **ONBOARDING.md**: repo role (Django+ninja API, strangler → FastAPI sidecar in
  `back/src/fastapp`), bootstrap order (agri-db `make upgrade-dev` → `back/.env` → `make up`), the
  api→core→db tag-pinned chain + `AGRI_DB_RO_TOKEN`, release flow (v-tags, semantic-release), CI =
  shared callers, deploy = push main → droplet, and the `deploy/nginx/back.conf` cutover pointer.

## Behavioral diffs found (diffed shared bodies vs the old inline ones)

**CI (old single "ruff" job → lint + test jobs):** 1. **Django system check env changed.** Old
  ci.yml ran `manage.py check` with `DEBUG=False, USE_POSTGRES=False` and no Postgres vars; the
  shared `python-test.yml` exports the caller `env_vars` before BOTH the check and pytest, so the
  check now runs with `DEBUG=True, USE_POSTGRES=True` + Postgres vars. `manage.py check` (without
  `--deploy`) doesn't hit the DB and raises no DEBUG-dependent errors, so the gate is equivalent —
  flagged for awareness. 2. **Job topology/duplication:** lint and test are separate runners, so `uv
  sync` runs twice (cache-shared) and the Postgres service no longer idles during lint. Check names
  change from `ruff` to `lint / ruff` + `test / pytest` (no branch protection on this repo, so
  nothing to retarget). 3. **Coverage flags:** old = `--cov` (source from pyproject
  `[tool.coverage.run] source=["src"]`, floor from `fail_under=85`); shared = `--cov=src
  --cov-fail-under=85`. Same measured tree (relative to `back/`), same floor; pyproject's
  `omit`/report settings still apply. The pyproject `fail_under` remains as defense in depth.

**Deploy (diffed `deploy-droplet.yml`'s remote script line-by-line against the old
  `deploy-back.yml`):** 1. `AGRI_DB_RO_TOKEN="$AGRI_DB_RO_TOKEN"` →
  `AGRI_DB_RO_TOKEN="${AGRI_DB_RO_TOKEN:-}"` — old form would abort under `set -u` if the env were
  ever absent; new form defaults empty. No change while the secret exists. 2. New no-op hook line `[
  -z "${PRE_DEPLOY_SCRIPT:-}" ] || eval "$PRE_DEPLOY_SCRIPT"` — inert (input left empty). 3. Migrate
  + build lines are env-var-parameterized (`"$MIGRATE_SERVICE" $MIGRATE_COMMAND`, `build
  ${BUILD_SERVICES:-}`) — with the inputs above they expand to the exact same commands (`build
  agri-api-web`, `run --rm --no-deps agri-api-web bash /code/docker-entrypoint.sh migrate`, `up -d
  --no-deps agri-api-web agri-api-worker agri-api-beat`, `docker image prune -f`). 4. Everything
  else identical: appleboy/ssh-action@v1.0.3, `script_stop: true`, `command_timeout: 20m`,
  single-line-only script discipline, same git fetch/reset. The caller adds an explicit
  `permissions: contents: read` (old file used the default token perms; the job only SSHes, so no
  effect). 5. The shared job forwards a few extra env names over SSH (`BUILD_SERVICES` etc.) —
  parameters only, no behavior change.

**Release/lint-pr-title/auto-assign:** shared bodies are verbatim copies of the old inline jobs
  (same action versions, same pinned MKS~ZAK author for the release commit); only `install_uv` is
  newly available and left off (agri-api's PSR `build_command` is empty).

## Verification - `yaml.safe_load` clean on all 5 workflow files. - CI on this PR runs the new
  `primary.yml` callers themselves — checks below prove the swap resolves `@v1` and stays green.

### Features

- **fastapp**: Verify Django-issued JWTs against the shared user table
  ([#297](https://github.com/AgriLogy/agri-api/pull/297),
  [`b8b1779`](https://github.com/AgriLogy/agri-api/commit/b8b177934262caae8551b0b0b4aeee9ac214f2b7))

Closes #296

> **Stacked on #295** (base = `feat/fastapp-scaffold`; auto-retargets to `main` when #295 merges).
  Merge #295 first.

## What Phase **F1** of the strangler migration: the FastAPI sidecar authenticates the exact `Bearer
  <access>` tokens the Django side mints with rest_framework_simplejwt — same key, same claims, same
  revocation semantics — so a route can cut over to :8001 without clients re-authenticating.

- **`back/src/fastapp/auth.py`** - PyJWT decode (direct dep already: `PyJWT==2.9.0`): HS256 +
  `settings.secret_key` (Django's `SECRET_KEY` = simplejwt's default SIGNING_KEY),
  `options={"require": ["exp", "iat"]}`; enforces the SIMPLE_JWT contract — `token_type == "access"`
  (TOKEN_TYPE_CLAIM) and a `user_id` claim (USER_ID_CLAIM). - User load through the agri-core
  SQLAlchemy session (`session_scope`) against agri-db's `CustomUserCustomuser`
  (`CustomUser_customuser` — the same table Django's auth reads); 401 for unknown or inactive users.
  - `token_session_revoked` ported **verbatim** from `agriapi/api/auth.py` (whole-second `iat` vs
  `sessions_revoked_at` compare, missing `iat` fails safe), with a comment pointing at the Django
  original — lifting both copies into agri-core is a later phase. - Dependencies `get_current_user`
  (HTTPBearer) / `get_current_staff_user` (403 unless `is_staff`) + typed `AuthedUser` (id,
  username, email, is_staff, preferred_language). - **`GET /fast/whoami`** — demo protected route
  returning the `AuthedUser`; harmless new path (nothing on the Django side serves `/fast/*`).

## Tests `back/src/fastapp/tests/test_auth_parity.py` — dual-ORM parity suite that mints REAL
  simplejwt tokens for a real Django user (pytest-django), then presents them to the fastapp
  TestClient. The SQLAlchemy lookup hits Django's test DB via the existing `_bind_agri_core_db`
  session fixture in `back/conftest.py`; `django_db(transaction=True)` commits rows so the separate
  SQLAlchemy connection sees them. Postgres-gated with the same skipif pattern as the other dual-ORM
  suites (skips on sqlite).

Covered: access token accepted (200 + exact payload) / refresh-type 401 / tampered signature 401 /
  `sessions_revoked_at` after iat 401 (+ the re-login flip side stays 200) / inactive user 401 /
  missing iat 401 / unknown user 401 / no credentials 401 / staff dependency 403-then-200.

## Verification - `ruff check` + `ruff format --check` clean. - Full fastapp suite run locally
  against a throwaway Postgres 18: **19 passed** (parity tests exercised for real, not skipped);
  `src/apps/users` regression run: **89 passed**. CI re-runs everything on its Postgres service.


## v1.79.0 (2026-07-02)

### Features

- **fastapp**: Scaffold FastAPI sidecar (no routes cut over)
  ([#295](https://github.com/AgriLogy/agri-api/pull/295),
  [`8394b7d`](https://github.com/AgriLogy/agri-api/commit/8394b7dec95d183c58c10ac9c10cf77fed1557e0))

Closes #294

## What Phase **F0** of the Django→FastAPI strangler migration: a sidecar FastAPI app that ships in
  the same pyproject/Docker image as Django and serves `:8001`, with **zero routes cut over** —
  Django keeps serving everything.

- **Deps:** `fastapi`, `uvicorn[standard]`, `pydantic-settings` (+ `httpx` in the dev group for
  TestClient); `uv.lock` updated via `uv add`. - **`back/src/fastapp/settings.py`** —
  pydantic-settings `AppSettings` reading the SAME env vars Django consumes (`SECRET_KEY`,
  `POSTGRES_*`/`AGRI_DB_URL`, `CELERY_BROKER_URL`, `RESEND_API_KEY`, `DEFAULT_FROM_EMAIL`,
  `CORS_ALLOWED_ORIGINS`, `DJANGO_ENV`) with dev defaults mirroring `agriapi/settings/dev.py`. No
  new variable names. - **`back/src/fastapp/errors.py`** — AgriError taxonomy identical to
  `agriapi/errors.py` (same codes + statuses) and exception handlers emitting the exact `{"error":
  {"code", "message"}}` envelope `agriapi/exception_handler.py` produces (including the `str(exc) or
  exc.code` fallback). - **`back/src/fastapp/main.py`** — `FastAPI(title="Agrilogy API (fastapp)",
  docs_url="/api/fast/docs")`, CORSMiddleware matching the django-cors-headers config (explicit
  origins, credentials, verbatim `default_headers`), `GET /healthz` returning `{"status", "app",
  "version"}` (version read from `back/pyproject.toml`), lifespan disposing the agri-core SQLAlchemy
  engine on shutdown. - **Entrypoint:** new `fast` role in `back/docker-entrypoint.sh` (waits for
  Postgres like `web`, then `exec uvicorn fastapp.main:app --host 0.0.0.0 --port 8001 --workers 2`;
  ensure/seed scripts stay web-only so the roles never race). - **Compose:** new `agri-api-fast`
  service mirroring `agri-api-web` (same image/build/secrets/env_file/volumes/network, publishes
  `8001:8001`, healthcheck on `/healthz`). - **`deploy/nginx/back.conf`** — faithful copy of the
  live `back.agrogo-datafarm.com` server blocks from the droplet (baseline: everything →
  `127.0.0.1:8000`), with a header documenting how cutover `location` blocks are added and rolled
  back. This file is the source of truth for strangler cutovers.

## Tests / gates - `back/src/fastapp/tests/test_healthz.py`: 9 tests (healthz payload, docs mount,
  all 6 error codes' envelope shape, empty-message fallback) — no DB needed; **9 passed** locally. -
  `ruff check src/` + `ruff format --check src/` clean; `bash -n docker-entrypoint.sh` clean;
  `docker compose config -q` valid.

## Follow-up (deliberately NOT in this PR) - Wiring `deploy/nginx/back.conf` auto-apply into
  `deploy-back.yml` — for now applying the nginx file on the droplet stays manual (`nginx -t &&
  systemctl reload nginx`). - F1 (JWT verification against the shared user table) lands on top of
  this branch.


## v1.78.1 (2026-07-01)

### Bug Fixes

- **admin**: Backfill each series from its own last reading
  ([#291](https://github.com/AgriLogy/agri-api/pull/291),
  [`8c5104d`](https://github.com/AgriLogy/agri-api/commit/8c5104df9616a0335e6435c24eed5a7efa623eba))

Closes #290

Auto backfill used a single global start (newest timestamp across all series). Once one series was
  current, stale series were skipped. Now, in auto mode, **each series extends from its own last
  reading**; an explicit `start` still applies to all. Adds a regression test with an uneven pair
  (one stale, one fresh). 8/8 pass.


## v1.78.0 (2026-06-30)

### Features

- **admin**: Sensor-data backfill to revive stale series
  ([#289](https://github.com/AgriLogy/agri-api/pull/289),
  [`10324fb`](https://github.com/AgriLogy/agri-api/commit/10324fba30f45bbf72fecf4cb5835402de312062))

Closes #288

Adds a staff-only backfill so an admin can revive a user/zone whose sensor feed stopped — filling
  the gap up to now from the back-office, **no SSH or code**.

### Endpoints (`/api/admin`) - `GET /users/{username}/zones/{zone_id}/backfill-status` — last
  reading, gap (hours), series-with-data. - `POST /users/{username}/zones/{zone_id}/backfill` —
  `{start?, end?, interval_minutes=60, dry_run}`.

### How it generates Schema-introspected: every model with `user`+`zone`+`timestamp` fields is a
  target (37 series). For each, the last real row is carried forward at the chosen interval, with
  light ±5% jitter on numeric columns, **skipping timestamps that already exist** (idempotent).
  Bulk-inserted under per-model/total caps. Audited. Empty zone → 400; already-current → 0 rows.

### Tests `test_admin_backfill.py` — 7 tests: auth (401/403), status gap, creates-rows +
  carry-forward, dry-run writes nothing, idempotent re-run, empty-zone 400. All green (14 incl.
  db-crud).

Powers the agri-admin **Backfill** tab (separate PR).


## v1.77.0 (2026-06-30)

### Chores

- **deps**: Bump agri-core 0.18.0 → 0.18.1 (pulls agri-db 0.11.1)
  ([#283](https://github.com/AgriLogy/agri-api/pull/283),
  [`1718490`](https://github.com/AgriLogy/agri-api/commit/17184901634a49be666bd389300900e14ac55362))

Closes #282. Final link in the db→core→api pin chain.

Bumps agri-core to **0.18.1**, which pins agri-db **0.11.1** — so the container's bundled Alembic
  head now matches the live schema (0.11.x) instead of lagging at 0.8.0. This is the prerequisite
  for turning on the deploy-time migration auto-apply (#281 / `docs/MIGRATIONS_PROD_CUTOVER.md`).

`uv lock` regenerated (agri-core 0.18.0→0.18.1, agri-db 0.8.0→0.11.1). Local suite **495 passed**;
  the 4 sqlite failures are the known Postgres-only dual-ORM tests that pass under CI's Postgres
  gate.

### Features

- **admin**: Generic database CRUD API over every model
  ([#287](https://github.com/AgriLogy/agri-api/pull/287),
  [`7ed3da3`](https://github.com/AgriLogy/agri-api/commit/7ed3da307d94dc926de2c6d11a42109028819a3d))

Closes #286

Adds a staff-only, schema-introspected CRUD API so **any** table is manageable from the admin
  console with **no per-table code** — new models appear automatically.

### Endpoints (`/api/admin/db`) - `GET /tables` — every model + row count - `GET
  /tables/{key}/schema` — field types, requiredness, choices, FK relations - `GET
  /tables/{key}/rows` — server-side search / sort / pagination - `GET|POST|PATCH|DELETE
  /tables/{key}/rows[/{pk}]` — full CRUD

`key` is `app_label.modelname`. All routes require JWT + `is_staff`; writes are audit-logged; bad
  input returns 400 (not 500); Django-internal bookkeeping tables are hidden.

### Tests `test_admin_db.py` — 7 tests: auth gating (401/403), introspection (tables/schema/FK),
  full CRUD lifecycle, and 400-on-invalid. All green.

Powers the agri-admin **Database** section (separate PR).


## v1.76.0 (2026-06-28)

### Features

- **deploy**: Auto-apply agri-db Alembic migrations on prod (default-off)
  ([#281](https://github.com/AgriLogy/agri-api/pull/281),
  [`ca41b9c`](https://github.com/AgriLogy/agri-api/commit/ca41b9c01b40b4e030b07e852e9e1c6b1fd70995))

Closes #280. Pairs with agri-db #44 (the empty-DB Alembic gate).

## What Prod schema changes can now flow through Alembic on deploy instead of hand-applied
  `ensure_*` scripts.

- **`migrate` entrypoint role** — resolves the live DB URL exactly as Django does (`AGRI_DB_URL` →
  `agrydata`, `postgresql+psycopg://…`) and runs the bundled `agri-migrate upgrade head`. -
  **`deploy-back.yml`** — runs `… docker-entrypoint.sh migrate` after build, **before** `up -d`, so
  new code never boots on an un-migrated schema. A failed migration aborts the deploy (`script_stop:
  true`). - **Default-off** — `RUN_DB_MIGRATIONS` unset → the step logs and exits 0. Merging this
  changes nothing on prod (verified locally). - **Safe legacy handling** — absent `alembic_version`
  refuses to replay from base; a one-time reconciliation needs an explicit `ALEMBIC_STAMP_REV`.

## Enabling (one-time) The container bundles agri-db **0.8.0** (via agri-core 0.18.0) while live is
  at **0.11.x**, and the DB is unstamped. Turning it on requires closing that pin gap
  (agri-db→agri-core→agri-api release chain) + a one-time stamp — full runbook in
  `docs/MIGRATIONS_PROD_CUTOVER.md`. That cutover is intentionally **not** in this PR.

## Verified `bash -n` clean; the default no-op path exits 0 without touching the DB. The active path
  can only be exercised once the cutover lands.


## v1.75.0 (2026-06-28)

### Features

- **alerts**: Derive type from sensor_key server-side
  ([#279](https://github.com/AgriLogy/agri-api/pull/279),
  [`a9c08af`](https://github.com/AgriLogy/agri-api/commit/a9c08af2a9493c82b84e29a45afd93c49c62466e))

Closes #278. Ticks the `type ↔ sensor_key consistency` box on #37.

## What The alert write path validated `sensor_key` (against `SENSOR_KEY_REGISTRY`) and zone
  ownership, but never enforced that an alert's `type` matched its `sensor_key`. A client could
  persist an inconsistent row, e.g. `type="Pressure"` + `sensor_key="soil_moisture_low"`.

## How `type` is now **server-authoritative**: `_apply()` derives it from the registry's canonical
  `type` for the alert's `sensor_key` on every create/update (`_canonical_type_for`).

- A mismatched `type` is normalized to the canonical one; an omitted `type` is filled in. -
  Re-derived on every write, so changing `sensor_key` keeps `type` consistent and legacy rows
  self-heal. - Alerts with no `sensor_key` (e.g. periodic maintenance) keep the client-supplied
  `type`. - Backward-compatible, **no schema change**.

## Tests 5 new cases in `AlertCRUDTests`: mismatch override, omitted-type derivation,
  PATCH-sensor_key re-derivation, and type-preserved-without-sensor_key. Full `test_alerts.py` green
  locally except the 3 pre-existing `AlertSuggestStrategyEndpointTests` (Postgres-only dual-ORM,
  pass in CI). Ruff lint + format clean.


## v1.74.3 (2026-06-26)

### Bug Fixes

- **deploy**: Attach agri-api to external agrilogy-back_agro network
  ([#277](https://github.com/AgriLogy/agri-api/pull/277),
  [`3913948`](https://github.com/AgriLogy/agri-api/commit/39139489733010b3dbb2421f4ed05385eb5ace7c))

Closes #276

Follow-up to #275. The rebuild fix worked (no more et_forecast crash), but recreating containers
  exposed a second issue: the agri-api compose 'agro' network was non-external, so recreated
  services landed on an isolated agri-api_agro network and lost connectivity to agrydata (DB) /
  redis / mailpit, which live on the legacy agrilogy-back_agro network → 502 'postgres not
  reachable'.

Fix: `agro` is now `external: true, name: agrilogy-back_agro`. Recreated containers join the network
  where the DB actually lives, matching the pre-existing (restart-preserved) wiring.


## v1.74.2 (2026-06-26)

### Bug Fixes

- **deploy**: Rebuild with AGRI_DB_RO_TOKEN instead of restart-only
  ([#275](https://github.com/AgriLogy/agri-api/pull/275),
  [`2b5e5c4`](https://github.com/AgriLogy/agri-api/commit/2b5e5c43b14efa552baa47a7cf6cc19d011bdd22))

Closes #274

Root cause of the current prod 502: the deploy workflow restarts containers without rebuilding, so
  this session's agri-core 0.18.0 bump (adds `agri.core.et_forecast`) left new bind-mounted code on
  the baked old venv → `ModuleNotFoundError: No module named 'agri.core.et_forecast'`.

Fix: the SSH deploy now rebuilds `agri-api:latest` with `AGRI_DB_RO_TOKEN` (the existing CI secret)
  as a build secret, then `up -d --no-deps` the three agri-api services. Build is layer-cached so
  code-only deploys stay fast; `--no-deps` avoids the mailpit/redis container-name conflict.

Merging this triggers a deploy run that rebuilds the droplet → cures the live 502 and establishes
  durable auto-deploy.


## v1.74.1 (2026-06-26)

### Bug Fixes

- **alerts**: Harden dispatch — skip channel-less alerts, nz-aware latest_value
  ([#273](https://github.com/AgriLogy/agri-api/pull/273),
  [`2ca8e17`](https://github.com/AgriLogy/agri-api/commit/2ca8e17cd94b782273afba60e4b8d653a7f39dde))

Closes #272

Post-merge cleanup from a convergence review of the alert-dispatch path (the review found **no
  critical/major bugs** — these are minor hardening items): - **Channel-less alert** no longer wins
  the grace claim: `dispatch_alerts_for_reading` skips an alert with
  `notify_email`/`notify_whatsapp`/`notify_sms` all false **before** the atomic claim, so it doesn't
  stamp `last_emailed_at`/`last_triggered_at` or count toward `enqueued` while sending nothing. -
  **`latest_value_for` is notification-zone-aware**: resolves the reading stream via the matching
  `NotificationZoneSensor.source_zone` (was silently falling through to user-wide scope). -
  **Docstring**: `dispatch_alerts_for_reading` return value documented correctly (alerts dispatched
  on any channel, not just emails). - **`available_sensors`**: logs the swallowed per-sensor
  exception instead of dropping it silently.

2 new tests (channel-less → 0 enqueued + no stamp; nz `latest_value` reads the source zone). Built
  in an isolated git worktree to avoid colliding with a parallel agent in the same checkout.

**Deliberately skipped** (noted as follow-ups): `grace_override_seconds` DB column type alignment
  (cosmetic, needs a migration); the `available_sensors` N×25 query optimization.


## v1.74.0 (2026-06-26)

### Features

- **weather**: 7-day reference-ET0 forecast endpoint (mock-first)
  ([#271](https://github.com/AgriLogy/agri-api/pull/271),
  [`14acb3d`](https://github.com/AgriLogy/agri-api/commit/14acb3d78cfdd7dfdc66c470cb4cdadc4b6dc61a))

Closes #270 (agrilogy-front #18)

`GET /weather/et-forecast?zone_id=&days=` → per-day ET0 forecast for the caller's zone. Daily
  weather from a swappable provider (`apps.sensors.forecast_provider`): deterministic seasonal
  **mock** by default (no key, reproducible), real **OpenWeather** stub behind
  `ET_FORECAST_PROVIDER=openweather` + `WEATHER_API_KEY` (documented follow-up). ET0 math = pure
  agri-core `et0_forecast` (pin 0.17.0→0.18.0). Owner-scoped, read-only. 5 endpoint tests (auth,
  7-day shape, days clamp, determinism, cross-user 404) green vs local Postgres.


## v1.73.0 (2026-06-26)

### Features

- **activegraph**: Default water_level_status to True + backfill
  ([#269](https://github.com/AgriLogy/agri-api/pull/269),
  [`1aaf3c6`](https://github.com/AgriLogy/agri-api/commit/1aaf3c636edd8622f9ad3b3ac1f30af76b7a3fdc))

Closes #268

Aligns the water-level dashboard section with every other ActiveGraph `*_status` flag (all default
  `True`). Model default flipped to `True`; Django migration `0066` does `AlterField(default=True)`
  + a `RunPython` backfill (`water_level_status=False → True`, no-op reverse). Schema-of-record
  change in agri-db (migration `c9d0e1f2a3b4`, **release 0.11.0**). Tests updated (new zone → True;
  admin can toggle off). Verified vs local Postgres.


## v1.72.0 (2026-06-25)

### Features

- **activegraph**: Water_level_status flag to gate the water-level section
  ([#267](https://github.com/AgriLogy/agri-api/pull/267),
  [`b0c339c`](https://github.com/AgriLogy/agri-api/commit/b0c339ce8a85d915f9b1940d6329eb06ccb0d3da))

Closes #266

Adds `ActiveGraph.water_level_status` (default `False` = opt-in; water-level is an uncommon
  basin/tank sensor) so the agri-web water-level dashboard section is toggled per zone like the
  other `*_status` flags. Reads flow through the existing `model_to_dict` response; the dynamic
  admin patch (`hasattr`/`setattr`) toggles it with no schema change. Django migration `0065`;
  schema-of-record in agri-db `b8c9d0e1f2a3` (PR #40). 2 tests (default-false + admin toggle), green
  vs local Postgres.

agrilogy-front #4 follow-up.


## v1.71.0 (2026-06-25)

### Features

- **alerts**: Strategy param on /alerts/suggest (mean/percentile/sd)
  ([#265](https://github.com/AgriLogy/agri-api/pull/265),
  [`d3b956d`](https://github.com/AgriLogy/agri-api/commit/d3b956d373e81b978e8c6aa6dec843930710cf2e))

Closes #264 — final open item of #37 (Item D).

Bumps **agri-core 0.16.0→0.17.0** (PR AgriLogy/agri-core#42, which added the percentile/SD threshold
  strategies) and adds an optional `?strategy=` query param to `GET /alerts/suggest`: - default
  `mean` (unchanged behaviour), - `percentile` (direction-aware p90/p10), - `sd` (mean ± 2σ).

Validated against `SUGGEST_STRATEGIES`; threaded through `engine.suggest_alert` →
  `suggest_alert_for`. 4 endpoint tests (echo per strategy + 400 on unknown); verified against local
  Postgres. Threshold math covered by agri-core's 11 unit tests.


## v1.70.0 (2026-06-25)

### Features

- **alerts**: Scheduled idle-zone liveness checker
  ([#263](https://github.com/AgriLogy/agri-api/pull/263),
  [`bd43011`](https://github.com/AgriLogy/agri-api/commit/bd43011f9bce11f03ce341df9051e373b0fa6b1f))

Closes #262 — third item from the #37 parking-lot.

New beat task `flag_idle_zones`: emails a zone's owner when its newest reading across all sensors is
  older than `ZONE_IDLE_THRESHOLD_HOURS` (default 24h, env-overridable). A Django-cache slot
  throttles re-flagging to once per `ZONE_IDLE_REFLAG_HOURS`. **Product call:** zones that have
  *never* reported are skipped — we only flag a zone that was alive and went silent (a sensor going
  offline), so empty/demo zones aren't emailed forever. Wired into `CELERY_BEAT_SCHEDULE` as
  `idle_zone_scan` (prod `DatabaseScheduler` needs a `PeriodicTask` row, same caveat as the other
  beat tasks). 3 tests; `manage.py check` + celery-routing green.


## v1.69.0 (2026-06-25)

### Features

- **alerts**: Aggregate same-reading alert emails into one digest
  ([#261](https://github.com/AgriLogy/agri-api/pull/261),
  [`215ff8e`](https://github.com/AgriLogy/agri-api/commit/215ff8e2485312e6a99e3a586d6a85244284dd24))

Closes #260 — second item from the #37 parking-lot.

When several alerts on the same `(user, sensor_key, zone)` reading opt into email,
  `dispatch_alerts_for_reading` now sends **one** combined digest (`send_alert_digest_email`)
  listing every triggered alert, instead of N separate emails. A single alert keeps the original
  per-alert email path unchanged; WhatsApp/SMS stay per-alert. The atomic per-alert grace claim
  (#160/#250) is untouched — emails are just collected and delivered once after the loop, so no
  dedup regression. 2 new tests; alert + notification-zone + ingest suites green on local Postgres.


## v1.68.0 (2026-06-25)

### Features

- **alerts**: Per-alert grace override (grace_override_seconds)
  ([#259](https://github.com/AgriLogy/agri-api/pull/259),
  [`cc50624`](https://github.com/AgriLogy/agri-api/commit/cc50624504f31eeca55447a75bb22e5117a1170f))

Closes #258 — first item from the #37 alerts-hardening parking-lot.

`Alert.grace_override_seconds` (nullable) lets one alert override the global
  `ALERT_GRACE_PERIODS[sensor_key]` re-notify cadence — e.g. ping-every-minute for a critical alert.
  NULL = global default. Used in `dispatch_alerts_for_reading`'s per-alert grace gate (override
  computed inside the loop), exposed on `AlertWriteIn` + the serializer. Django migration 0064;
  schema-of-record in agri-db #38 (`a7b8c9d0e1f2`). 2 new dispatch tests; full alert suite green
  (61) on local Postgres.


## v1.67.0 (2026-06-25)

### Features

- **ingest**: Single-sensor webhook for water-level (and any registry sensor)
  ([#257](https://github.com/AgriLogy/agri-api/pull/257),
  [`cf2ce7f`](https://github.com/AgriLogy/agri-api/commit/cf2ce7f209c9250b1b6cbb124f5fa8fb433ded11))

Closes #4 — and resolves the "extend push beyond the 6 weather metrics" item in the #37
  alerts-hardening parking-lot.

**Finding:** the `water_level` sensor_key, the `WaterLevelSensor` model (agri-api + agri-db mirror),
  and the alert/threshold path **already existed** — and `/ingest/weather` is already
  registry-generic. The only real gap was a clean device-facing writer for standalone sensors. **No
  agri-db change needed.**

**Added:** `POST /ingest/sensor` — a typed, one-reading webhook `{client, sensor_key, value,
  timestamp?}` (auth=None, same client-identification convention as `/ingest/weather`) for
  tank/basin/water-level probes that don't speak the multi-metric bridge payload. It validates
  `sensor_key` against `SENSOR_KEY_REGISTRY` (NPK excluded — three-value model), persists to the
  caller's zone, and pushes the reading through `dispatch_alerts_for_reading` so low/abnormal-level
  alerts fire on ingest.

**Acceptance criteria (#4):** data received + stored ✓; alert on threshold breach ✓ (verified by
  test). "View current water level" is already served by the generic `/admin/sensor-data` explorer
  (#234); the **end-user dashboard tile is an agri-web follow-up**.

7 tests (stored / fires alert — asserts both `last_emailed_at` + email outbox / above-threshold
  no-fire / unknown key / npk / unknown client / missing value), green against local Postgres.


## v1.66.0 (2026-06-25)

### Features

- **users**: Per-user notification language (fr/ar)
  ([#256](https://github.com/AgriLogy/agri-api/pull/256),
  [`95cfc3a`](https://github.com/AgriLogy/agri-api/commit/95cfc3a10fdd0088c2e5848c114accc97a4ee005))

Closes #31.

A per-user `preferred_language` (fr|ar, default fr) that drives the daily notification email's
  language. - `CustomUser.preferred_language` + Django migration `0009` (schema-of-record agri-db
  0.8.0, migration `f3a4b5c6d7e8`). - Self-service: `GET /users/me` returns it, `PATCH /users/me`
  lets the caller set it (validated fr/ar). Admin patch `/users/{username}` + the detail serializer
  carry it too. - The periodic email picks it up automatically: `perform_calculations` → agri-core
  0.16.0 `compose_notification_for_user` renders FR or AR. agri-core pin bumped 0.15.0→0.16.0. - 6
  tests (self default/set/invalid, admin set/invalid, anon 401); 16 green incl. existing
  admin-detail.

⚠️ The Arabic notification copy (in agri-core) is a faithful first draft and needs native-speaker
  review. The agri-web language-toggle UI is a deliberate follow-up.


## v1.65.0 (2026-06-25)

### Features

- **alerts**: Custom notification zones + per-alert SMS channel
  ([#255](https://github.com/AgriLogy/agri-api/pull/255),
  [`4813337`](https://github.com/AgriLogy/agri-api/commit/4813337bdff444c39286efe308b6e073fdb110c5))

Closes #254 (implements agrilogy-front #57 on the API).

User-owned **notification zones** independent of farm zones. An alert binds to a farm zone **XOR** a
  notification zone, whose data stream resolves through `(sensor_key, source_zone)` assignments.

- Unmanaged `NotificationZone` + `NotificationZoneSensor` (schema in agri-db 0.7.0; self-deploy via
  `scripts/ensure_notification_zone_tables.py` + conftest + entrypoint). `Alert` gains
  `notification_zone` FK + `notify_sms`. - Django migration `0063` (CreateModel state-only for the
  unmanaged tables + AddField for the alert columns). - `router_notification_zones`: user-scoped
  CRUD + sensor assign/unassign + `/available-sensors`. `router_alerts` accepts `notification_zone`
  (XOR `zone`) + `notify_sms` with ownership validation. - `dispatch_alerts_for_reading` matches
  notification-zone alerts via their sensor assignment (specific `source_zone` or any-zone), **not**
  as user-wide. - **SMS**: `send_alert_sms` (Twilio via `agriapi.twilio_messaging`) wired into the
  fan-out gated on `notify_sms` — the agri-web notify_sms toggle is now real. - Bumps agri-core pin
  `0.14.0→0.15.0`.

11 new tests (zone CRUD, available-sensors, alert binding + XOR validation, dispatch fires for
  assigned source zone only, SMS fan-out); existing alerts + affirmation suites green against
  Postgres.

**Deploy:** apply agri-db 0.7.0 (`make upgrade-dev`/`upgrade-prod`) first; set `TWILIO_SMS_FROM` on
  the droplet for SMS.


## v1.64.0 (2026-06-25)

### Features

- **zone**: Persist elevation_m so Rso is correct away from sea level
  ([#253](https://github.com/AgriLogy/agri-api/pull/253),
  [`ade7920`](https://github.com/AgriLogy/agri-api/commit/ade7920ba6b61cef9c72e9749c7a219059e73507))

Closes #15

Final step of the elevation_m chain (agri-db AgriLogy/agri-db#32 → 0.6.0, agri-core
  AgriLogy/agri-core#36 → 0.14.0).

- `Zone.elevation_m` FloatField (default 0, metres) + Django migration `0062`. - Exposed on the
  admin zone API: added to `ZoneWriteIn` + `ZONE_FIELDS` so create/update/serialize round-trip it. -
  Bumps agri-core pin `0.13.0 → 0.14.0` (reads `zone.elevation_m` into `Et0Inputs`; `Rso = (0.75 +
  2e-5·elevation_m)·Ra`).

64 irrigation+agronomy+affirmation tests green locally against Postgres.

**Deploy:** apply agri-db#32 (`make upgrade-dev`/`upgrade-prod`) — it also merges the 3 diverged
  alembic heads — before this rolls out.


## v1.63.0 (2026-06-25)

### Features

- **affirmation**: Apply payload to the resource on approve
  ([#252](https://github.com/AgriLogy/agri-api/pull/252),
  [`007a9d3`](https://github.com/AgriLogy/agri-api/commit/007a9d3058775df223ae4c10c63d36e198aec28a))

Closes #25

Follow-up to #20: the `ManagerAffirmation` model + create/list/decide endpoints existed, but
  approving never *applied* the requested change.

Adds `apps.irrigation.affirmation_appliers.apply_affirmation()`, dispatched on `action` from the
  approve branch of `_decide` inside `transaction.atomic`: - **zone_params_change** `{zone_id,
  fields}` — validate against the writable param allowlist + `router_admin._validate_zone` + zone
  ownership (`requested_by`), then update the `Zone`. - **kc_periods_change** `{kc_id, periods[]}` —
  replace the crop calendar's periods via `router_kc._replace_periods`. - **user_reactivate**
  `{user_id}` — set the target user active.

A recognised action with no actionable payload is a **no-op approval** (legacy/empty affirmations
  stay approvable); a non-empty **malformed** payload raises `AffirmationApplyError` → `400` and the
  affirmation stays `pending` (transaction rolled back).

6 new tests (approve applies / reject leaves unchanged / invalid field 400+pending / cross-user 400
  / kc periods replaced / user reactivated). Full `test_manager_affirmation.py` suite green (16)
  against local Postgres.


## v1.62.0 (2026-06-25)

### Features

- **alerts**: Per-alert email/WhatsApp delivery via Twilio
  ([#251](https://github.com/AgriLogy/agri-api/pull/251),
  [`35b3bca`](https://github.com/AgriLogy/agri-api/commit/35b3bca3d0ed464f993773d2cb45b24a2b5b9e77))

Closes #162

Alerts now fan out to the channels the owner opted into.

- New `notify_email` (default `True`) / `notify_whatsapp` (default `False`) fields on `Alert` —
  Django migration `0061`, schema-of-record in agri-db (`c4d8e1f02a37`, PR AgriLogy/agri-db#30). -
  ninja write schema (`AlertWriteIn`) + serializer expose both fields. -
  `dispatch_alerts_for_reading` enqueues `send_alert_email` and/or `send_alert_whatsapp` after the
  existing atomic grace-period claim, so dedup behaviour is preserved. - WhatsApp delivery via a
  stdlib-only Twilio sender (`agriapi.whatsapp`, no SDK) to the owner's `phone_number`; creds from
  `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_FROM`. Missing creds or no phone =
  logged no-op, never raises (Celery won't retry+dupe). - Legacy alerts keep email-only behaviour.

Supersedes #163 (which was stacked on the now-closed #161 and never went green); rebased clean onto
  main with `0061` chained off `0060` and ruff-format applied.

**Deploy:** apply agri-db#30 (`make upgrade-dev`/`upgrade-prod`) first, then set `TWILIO_*` on the
  droplet `.env`.


## v1.61.1 (2026-06-25)

### Bug Fixes

- **notifications**: Prevent duplicate periodic emails via atomic claim
  ([#250](https://github.com/AgriLogy/agri-api/pull/250),
  [`32fd8bb`](https://github.com/AgriLogy/agri-api/commit/32fd8bb55ebcadda84cbf79b89e2ce2606087767))

Closes #160

## Problem `send_periodic_notifications` gated on `should_notify(user)` (reads `last_notified`),
  then **separately** sent the email and saved `last_notified`. Two concurrent Celery beat runs both
  read the stale timestamp, both pass the gate, and **both send** the same digest.

## Fix New `claim_notification_slot(user)` runs a single conditional UPDATE that stamps
  `last_notified = now` only when the cadence window has actually elapsed (`last_notified IS NULL OR
  < cutoff`). Exactly one concurrent run's UPDATE matches a row and wins; the loser is skipped. The
  clock is advanced up-front, so a provider failure does not re-attempt the user on every tick —
  preserving the policy from #180. Mirrors the alert engine's `last_emailed_at` claim.

## Tests (run on Postgres locally — dual-ORM/transaction class) -
  `test_atomic_claim_prevents_double_send` — two consecutive runs send exactly **one** email. -
  `test_claim_helper_only_one_winner` — helper returns True then False. - Existing
  `test_failed_send_still_advances_last_notified` (#180) still green.

Supersedes the stale, conflicting PR #161 (its branch predates the merged #180 and no longer applies
  to main).

### Continuous Integration

- Enforce ruff format --check ([#249](https://github.com/AgriLogy/agri-api/pull/249),
  [`1b18713`](https://github.com/AgriLogy/agri-api/commit/1b187132dbdb84e0b1d5245ad43a4ddff4219b8f))

Closes #26

The backend CI lint job ran `ruff check` but skipped `ruff format --check` behind a comment noting
  the legacy codebase had never been fully formatted. The tree is now clean, so this:

- runs `ruff format` on the 8 residual files (pure line-rewraps, no logic change) - adds a `ruff
  format check` step right after `ruff check` - removes the stale "intentionally not enforced"
  comment

Verified locally: `uv run ruff check .` and `uv run ruff format --check .` both pass.


## v1.61.0 (2026-06-24)

### Features

- **cors**: Allow admin/identity dev origins + document prod SSO origin list
  ([#248](https://github.com/AgriLogy/agri-api/pull/248),
  [`1df4d78`](https://github.com/AgriLogy/agri-api/commit/1df4d78ba71885bb128728f915c474d6b79205d1))

Closes #247

- `settings/dev.py`: adds localhost/127.0.0.1 `:3001` (admin) and `:3002` (identity) to the dev
  CORS/CSRF defaults. Admin's browser axios genuinely needs `:3001`; the gateway talks to the API
  server-side so `:3002` is defensive. - `docs/SSO_CORS_ORIGINS.md`: precise prod env checklist
  (`CORS_ALLOWED_ORIGINS`/`CSRF_TRUSTED_ORIGINS` = app./admin./identity./www.) for the droplet,
  since prod reads these from env with no committed default.

ruff lint + format clean.


## v1.60.0 (2026-06-24)

### Chores

- Remove dead agriapi/api/routers package
  ([`d15b68b`](https://github.com/AgriLogy/agri-api/commit/d15b68b2026e4a4b0c5b4e959bb2c76268cea404))

agriapi/api/routers/sensors.py was orphaned by the DRF->django-ninja migration: it is never imported
  (the empty routers/__init__.py doesn't export it) and the live /sensors endpoint is served by
  apps/sensors/router_sensors.py (mounted in agriapi/api/__init__.py). No tests reference it.
  Removing the dead package.

- Remove dead agriapi/api/routers package ([#244](https://github.com/AgriLogy/agri-api/pull/244),
  [`28b8df5`](https://github.com/AgriLogy/agri-api/commit/28b8df5a05cbc5e06be7168000ad317b8848137d))

### Features

- **auth**: Self-service log-out-everywhere endpoint (DELETE /auth/sessions)
  ([#246](https://github.com/AgriLogy/agri-api/pull/246),
  [`64bade8`](https://github.com/AgriLogy/agri-api/commit/64bade89fd135dfcbbcb1b24e527af4358ebd2c0))

Closes #245

Adds the user-driven twin of the admin `force-logout`: `DELETE /auth/sessions` bumps the caller's
  own `CustomUser.sessions_revoked_at`, so every token issued so far is rejected — the current
  access token fails on its next request and the refresh token can no longer mint a new one. This is
  what makes one "log out" end the session across every Agrogo app sharing the SSO session, not just
  the active tab.

No schema change (the column already exists in agri-db). ruff lint + format clean.

Pairs with agri-web #12 / agri-admin #34, which call this best-effort on logout before clearing the
  shared cookie.

### Testing

- Cover admin session revocation; floor iat comparison
  ([#242](https://github.com/AgriLogy/agri-api/pull/242),
  [`2b7f327`](https://github.com/AgriLogy/agri-api/commit/2b7f327820c0ef1edb9f05e45607258dedcda6b6))

- Cover session revocation; floor iat comparison to whole seconds
  ([`f428d5c`](https://github.com/AgriLogy/agri-api/commit/f428d5c11e4174003d9fe86c7f4def09c6754dbb))

Add a full suite for the admin session kill switch (37 cases): token_session_revoked predicate,
  JwtAuth (ninja) + RevocationAwareJWTAuthentication (DRF) + the refresh view, GET /sessions status,
  POST /force-logout, and disable/soft-delete also revoking.

Writing the tests surfaced a sub-second edge: JWT iat is integer-second while sessions_revoked_at
  has microsecond precision, so a re-login in the same second as a force-logout could be falsely
  killed. Compare at whole-second resolution (iat < int(revoked_at.timestamp())) in both auth layers
  to fix it.


## v1.59.0 (2026-06-21)

### Features

- Admin session revocation (force logout / disable)
  ([`aa2df18`](https://github.com/AgriLogy/agri-api/commit/aa2df183651d7f5a87670cd8a2c8e3b6608b3458))

Add a per-user session kill switch backed by CustomUser.sessions_revoked_at. Any access or refresh
  token issued before that timestamp is rejected, so an admin can force a user to log out
  immediately — the current access token dies on the next request and the refresh token can no
  longer mint a new one.

- token_session_revoked() enforced in both JwtAuth (django-ninja) and a new
  RevocationAwareJWTAuthentication (DRF default), plus a revocation-aware token-refresh view. -
  Admin endpoints: GET /users/{u}/sessions (token status) and POST /users/{u}/force-logout.
  Disabling an account now also revokes. - Mirrors agri-db column with Django field + migration
  0008.

- Admin session revocation (force logout / disable)
  ([#240](https://github.com/AgriLogy/agri-api/pull/240),
  [`9a76394`](https://github.com/AgriLogy/agri-api/commit/9a76394bca420b97dc1648ffd7b86a9b39763769))


## v1.58.0 (2026-06-19)

### Features

- **admin**: Global Kc CRUD, system-setting create/delete, per-user display config
  ([#236](https://github.com/AgriLogy/agri-api/pull/236),
  [`59ad656`](https://github.com/AgriLogy/agri-api/commit/59ad656b1de028f4f37e5784deeae71415748c43))

- **admin**: Read-only view-as impersonation (token + mutation block)
  ([#238](https://github.com/AgriLogy/agri-api/pull/238),
  [`7fef989`](https://github.com/AgriLogy/agri-api/commit/7fef98952630b86ec0d3f3a8107fffd195c2a8da))

- **admin**: Sensor-data explorer API + device-health tables
  ([`292b297`](https://github.com/AgriLogy/agri-api/commit/292b297ac55db90a27d6b4700387156f18274ee7))

Generic admin surface over the 45 sensor models (resolved via SENSOR_MODELS) so the back-office can
  browse/correct/clean raw time-series for any user/zone without 45 CRUD pages:

- router_sensor_data @ /admin/sensor-data (is_staff, audited): GET /catalog, GET list
  (sensor/username/zone/from/to/limit), PATCH {sensor}/{id} value, DELETE {sensor}/{id}, DELETE
  {sensor} range (guarded: zone_id + time bound). - ensure_sensor_health_tables.py self-deploys
  BatterySensor/SignalSensor on boot (migration 0060 exists but agri-db baseline may predate them).

NpkSensor appears in the catalog but is read-only here (no single value field).

Closes #233

- **admin**: Sensor-data explorer API + device-health tables
  ([#234](https://github.com/AgriLogy/agri-api/pull/234),
  [`1f5c662`](https://github.com/AgriLogy/agri-api/commit/1f5c662ef064e574bc22a14f52f90e51efe87f46))


## v1.57.0 (2026-06-19)

### Features

- **admin**: Records CRUD — notifications, conversations, proactive, grants
  ([`3fff8fa`](https://github.com/AgriLogy/agri-api/commit/3fff8fade0eac18f3d23edb686b330d4b70e17d9))

New router_records mounted at /admin (JWT + is_staff, all audited):

- notifications: GET (filter by user) + DELETE - conversations: GET list + GET {pk} (with
  transcript) + DELETE - proactive-notices: GET + DELETE (reset a user's cooldown) -
  technician-grants: GLOBAL GET across all owners (the /technicians router is owner-scoped) + GET
  {pk} (+scope) + PATCH (enable/disable) + DELETE.

No new tables — all back existing models. test_admin_records.py covers 403-gating, list/filter,
  detail, delete and grant patch/revoke.

Closes #231

- **admin**: Records CRUD — notifications, conversations, proactive, grants
  ([#232](https://github.com/AgriLogy/agri-api/pull/232),
  [`4de4511`](https://github.com/AgriLogy/agri-api/commit/4de45119fcdd8223f7f11d1eb62c5fb1dee62eb3))


## v1.56.0 (2026-06-19)

### Features

- **admin**: Create global alerts (POST /admin/alerts)
  ([`b69aef6`](https://github.com/AgriLogy/agri-api/commit/b69aef6a1c425edfcc168bfb9fb0d6aabf422bcf))

Completes the global alerts console CRUD — it could list/toggle/delete but not create. Adds POST
  /admin/alerts (is_staff, audited): resolves the target user, validates the optional zone belongs
  to them, and creates the alert
  (name/type/condition/condition_nbr/sensor_key/description/zone/is_active).

The manager-affirmation approve/reject workflow already exists (router_manager_affirmation), so no
  backend change there.

Closes #229

- **admin**: Create global alerts (POST /admin/alerts)
  ([#230](https://github.com/AgriLogy/agri-api/pull/230),
  [`d808f57`](https://github.com/AgriLogy/agri-api/commit/d808f578467264b4e3cfe20c1019ba7599705ec3))

- **admin**: Global Kc CRUD, system-setting create/delete, per-user display config
  ([`7a90188`](https://github.com/AgriLogy/agri-api/commit/7a90188e8baf06a9c29d6bbcba2c2ccd7d38ffb0))

- router_admin_kc: /admin/kc list (filter by username/zone) + create-for-user + detail + replace +
  delete, reusing the owner router's serialize/period helpers. Cross-user management for staff (the
  /kc router stays owner-scoped). - router_settings: add POST (create key, 409 on dup) + DELETE
  /{key} alongside the existing GET/PATCH. - router_admin: add GET/PATCH for per-user/zone
  graph-names + sensor-colors (mirrors the existing active-graph admin endpoints).

All is_staff-gated + audited; no new tables. 8 new tests; analytics suite green.

Closes #235

- **admin**: Read-only view-as impersonation (token + mutation block)
  ([`981bfcd`](https://github.com/AgriLogy/agri-api/commit/981bfcd28fc1cee705c8cec779c388be356c52d2))

Lets a staff user start a short-lived read-only session as any user so the admin can see what that
  user sees without being able to change anything.

- POST /admin/impersonate/{username} (is_staff, audited): mints a 30-min simplejwt access token
  authenticating AS the target user with a readonly claim + impersonator metadata. -
  ImpersonationReadOnlyMiddleware: rejects any non-safe HTTP method made with a readonly token
  (403), before routing — covers django-ninja and DRF alike, and blocks chaining a new impersonation
  from a readonly token.

Closes #237

- **monitoring**: Task-run, delivery & login history + /admin/monitoring API
  ([`91e25ae`](https://github.com/AgriLogy/agri-api/commit/91e25ae7178e4068e6cb27560a68e7bc6822db1d))

Adds the observability foundation for the admin back-office. Three new unmanaged tables
  (self-deployed via ensure_monitoring_tables.py):

- TaskRun: one row per Celery task execution, captured centrally from task_postrun/task_failure
  signals in agriapi.celery (no per-task edits). - NotificationDeliveryLog: one row per
  email/SMS/WhatsApp delivery attempt, recorded by the senders in agriapi.tasks via a fail-soft
  record_delivery(). - LoginEvent: one row per sign-in attempt, recorded in the /auth/sessions path.

New router_monitoring (JWT + is_staff) mounted at /admin/monitoring: overview (24h KPIs + recent
  failures + fleet), tasks (history + 7d aggregates + beat schedule), deliveries, logins — all
  filterable.

Closes #227

- **monitoring**: Task-run, delivery & login history + /admin/monitoring API
  ([#228](https://github.com/AgriLogy/agri-api/pull/228),
  [`b2415e9`](https://github.com/AgriLogy/agri-api/commit/b2415e9eb251a4330a90f580be21c3f6ca74a372))


## v1.55.0 (2026-06-19)

### Features

- **irrigation**: Scheduled programs + manual valve/pump commands (simulated)
  ([#226](https://github.com/AgriLogy/agri-api/pull/226),
  [`42fdddd`](https://github.com/AgriLogy/agri-api/commit/42fdddd2016447dda6abc2c3d52618b0a1a4f4b7))

Closes #225

The software half of irrigation automation. **No hardware is actuated** — there is no downlink path
  in the codebase, so physical dispatch is gated behind `IRRIGATION_DISPATCH_ENABLED` (default
  **false**): commands are recorded as `simulated` and only logged. The real ChirpStack/Bivocom
  command path is a deliberate `NotImplementedError` seam to wire + hardware-test before the flag is
  turned on.

## Backend - `IrrigationProgram` + `OutputCommand` models (managed=False self-deploy template:
  `ensure_irrigation_tables.py` wired in `docker-entrypoint.sh` + root conftest + analytics
  re-export). - `/irrigation` router (JwtAuth, caller-scoped, technicians blocked from writes):
  programs CRUD; `POST /commands` (manual open/close → dispatch); `GET /commands` history; `GET
  /config` (exposes `dispatch_enabled` so the UI shows a simulation banner). -
  `output_dispatch.dispatch_command()` — the safety seam (simulate, or raise if
  enabled-but-unwired). - `run_due_irrigation_programs` beat task — fires due programs once per
  window (weekday + start_time), atomic per-window dedup. Beat entry `irrigation_run` (needs a
  `PeriodicTask` row on prod, like `email_ping`).

## Verify `pytest src/analytics/tests/test_irrigation_automation.py` **8 passed** (programs CRUD +
  isolation, manual command simulated, bad-action, technician-block, config, scheduler
  due/dedup/disabled/weekday) · ruff + format + `manage.py check` clean. Postgres CI pending on this
  PR.


## v1.54.0 (2026-06-19)

### Features

- **assistant**: Write-actions + proactive irrigation nudges
  ([#224](https://github.com/AgriLogy/agri-api/pull/224),
  [`7ab9534`](https://github.com/AgriLogy/agri-api/commit/7ab953495b4ee85edbf18aa8cb009ba53656ec08))

Closes #223

Completes Feature 3 (the page-aware half shipped separately in agrilogy-front #228).

## Write-actions (mutating tools) - `create_alert` and `set_notification_cadence` added to the
  assistant ToolRegistry with a `mutating` flag. Both execute as the calling user (run_tool passes
  request.auth), are caller-scoped, **block technicians**, validate input, and are reversible — the
  result states what changed so the assistant can tell the user how to undo it. Auto-exposed to the
  LLM tool-calling loop.

## Proactive insights - `scan_proactive_insights` Celery task: per active customer, reuses
  `_get_irrigation_advice` and emails a nudge when the recommendation is *irrigate*. **Dedup** =
  atomic `ProactiveNotice.last_sent` claim with a 24h cooldown (rolled back on send failure to
  retry). Beat entry `proactive_scan`. - `ProactiveNotice` is a `managed=False` model
  (db_constraint=False FK) that **self-deploys** via `ensure_assistant_tables.py` (already
  entrypoint-wired) and is created session-wide in `conftest.py` — no prod migration.

## Activation note Prod beat runs the DatabaseScheduler, so `proactive_scan` (and
  `device_health_scan`) need a `django_celery_beat` PeriodicTask row to fire on prod — same as
  `email_ping`.

## Verify `pytest src/apps/assistant/` **82 passed** (new write-tool + proactive tests incl. dedup,
  hold=no-email, staff/technician skip) · analytics flush slice 197 passed (the 5
  `TestZoneNotificationOutbound` failures are pre-existing env/Celery, not regressions) · ruff +
  format + `manage.py check` clean.


## v1.53.0 (2026-06-19)

### Features

- **devices**: Device registry + device-health alerts
  ([#222](https://github.com/AgriLogy/agri-api/pull/222),
  [`043f405`](https://github.com/AgriLogy/agri-api/commit/043f405489121013162f4f6d30abd8659b9e9b0c))

Closes #221

## Device registry (revives the dead admin page) `/devices` admin CRUD (`GET`/`POST`,
  `PATCH`/`DELETE /{pk}`, `is_staff`) over a new `Device` model. The agri-admin
  `adminDeviceApi`/`DevicesMain` page already calls these exact paths but had **no backend on main**
  (the registry was stranded on the unpushed `feat/battery-signal-metrics` branch) — this revives it
  with **no UI change**. - Isolated from the battery/signal work; the **ingest-routing** edits
  (bivocom/chirpstack webhooks) were intentionally trimmed to keep the diff to the registry CRUD +
  model. - New table uses the proven self-deploy template: `managed=False` + `db_constraint=False`
  FKs + `scripts/ensure_device_tables.py` (wired in `docker-entrypoint.sh`) + session-wide
  `conftest` creation. No prod-DB migration needed.

## Device-health alerts (net-new) - `scan_device_health` Celery task + a pure
  `classify_device_health` helper: flags **offline** (no uplink > 24h) and **low-battery** (< 3.4V)
  from each device's latest LoRaWAN uplink and emails the owner. - **Dedup** via an atomic
  `last_health_notified` claim (cooldown 24h) so a device is emailed at most once per window even
  across concurrent beat ticks (mirrors the periodic-notification claim pattern). On send failure
  the claim is rolled back to retry next tick. - Beat entry `device_health_scan` added (hourly in
  prod). NOTE: prod beat runs the **DatabaseScheduler**, so a `django_celery_beat` PeriodicTask row
  must be created to activate it (same as `email_ping`) — the task is otherwise complete + testable.

## Verify - 15 device tests (CRUD + classifier + scan dedup) green; 37-test
  device+technician+lorawan slice green (no flush regression). ruff + format + `manage.py check`
  clean. Postgres CI gates the flush.


## v1.52.0 (2026-06-19)

### Features

- **kc**: Crop-calendar CRUD endpoints (/kc) ([#220](https://github.com/AgriLogy/agri-api/pull/220),
  [`d16de28`](https://github.com/AgriLogy/agri-api/commit/d16de2891cf7b2bbdb8e40bf61385adb9cad56a2))

Closes #219

Per-user `/kc` CRUD over the existing **Kc/KcPeriod/KcPeriodAssignment** models (already in the
  agri-db schema, migration 0028 — **no self-deploy needed**, managed tables).

**Endpoints** (JwtAuth, caller-scoped, technicians blocked): - `GET /kc?zone_id=` → list
  `[{id,name,plant_name,zone_id,zone_name,number_of_periods,periods:[{id,period_name,start_date,end_date,kc_value}]}]`
  - `POST /kc` → create (name, plant_name, zone_id?, periods[]) → 201 - `GET/PUT/DELETE /kc/{id}` —
  PUT replaces fields + periods; DELETE cleans up periods

**Decision:** direct CRUD (not affirmation-gated) — the affirmation `_decide` only records status
  with an opaque payload, it doesn't apply changes, so gating Kc through it would add a no-op
  governance layer.

**Verify:** 6 tests pass · ruff clean · `manage.py check` clean.


## v1.51.0 (2026-06-19)

### Features

- **admin**: Billing, audit log, and system settings backend (Wave 4)
  ([#218](https://github.com/AgriLogy/agri-api/pull/218),
  [`c0603c0`](https://github.com/AgriLogy/agri-api/commit/c0603c0852b2bcc307de1f280f5e75a80614bc3b))

Closes #217

Wave 4 of the business-admin console — three modules, no new Django apps (unmanaged tables in the
  analytics namespace, the proven assistant/technician template).

## Billing (E) `Plan`/`Subscription`/`Invoice` + `/admin/billing/{plans,subscriptions,invoices}`
  CRUD (is_staff). Assigning/cancelling a subscription mirrors `CustomUser.payement_status`
  (active→actif, else suspended). Invoices support create + mark-paid.

## Audit (F) `AuditEvent` + a fail-soft `record_audit()` helper, wired into billing, settings,
  **zone create/delete**, and **technician create/revoke**. `GET /admin/audit` with
  actor/action/target filters.

## Settings (G) `SystemSetting` key/value store, `GET/PATCH /admin/settings` grouped by category,
  defaults seeded lazily on first GET.

## Schema handling All three tables are `managed=False` + `db_constraint=False` FKs (keeps the
  postgres CI flush green), **self-deploy on prod** via `scripts/ensure_admin_tables.py` (wired into
  `docker-entrypoint.sh` after the technician one), and are created session-wide in the test DB by
  the root conftest.

## Verify 17 new tests (billing CRUD + payment sync, audit record + filter, settings seed + upsert,
  all 403-gated) + technician/analytics suites pass · ruff + format clean · `manage.py check` clean.
  Postgres CI pending below.


## v1.50.0 (2026-06-19)

### Features

- **admin**: Device fleet health endpoint (LoRaWAN uplinks)
  ([#216](https://github.com/AgriLogy/agri-api/pull/216),
  [`702cf8d`](https://github.com/AgriLogy/agri-api/commit/702cf8d8e5ceb68d99b1c1b858b73078dc75baf8))

Closes #215

Adds `GET /api/admin/devices/health` (JwtAuth + is_staff): aggregates `LoraUplink` by `dev_eui` →
  latest device_name, last-seen, battery_v, rssi/snr, 24h uplink count, and an online (<24h) / stale
  (<72h) / offline status, plus a fleet summary {total, online, stale, offline}.

Read-only, no new table; the LoRaWAN aggregation is best-effort (returns an empty fleet rather than
  500 when the `lora_uplink` table is absent, e.g. the sqlite test DB).

**Deviation:** no `/devices` registry/Device model exists on agri-api `main` (that's part of the
  unmerged device-registration work, like the technician backend was), so dev_eui can't be joined to
  an assigned user/zone yet — the endpoint surfaces uplink-derived identity (dev_eui + device_name)
  only.

Verify: 2 tests pass (non-staff 403 + admin fleet shape) on sqlite; ruff + format clean; manage.py
  check clean.


## v1.49.0 (2026-06-19)

### Features

- **admin**: Business analytics + global alerts endpoints
  ([#214](https://github.com/AgriLogy/agri-api/pull/214),
  [`f4bf22a`](https://github.com/AgriLogy/agri-api/commit/f4bf22a48226f3d221df25a30219aa79a1fc47b3))

Closes #213

Read-only, is_staff-gated admin aggregation endpoints for the new console dashboard + alerts console
  (Wave 2, module B+C). No new tables.

## Endpoints - **GET /admin/analytics** — `{payment_status, signups_by_week, active_users,
  inactive_users, zones_per_user, alerts_by_type, devices:{total,stale,online}}`. Customers =
  non-staff, non-technician users. Device health is best-effort (tolerates a missing `lora_uplink`
  table). - **GET /admin/alerts?username&type&sensor_key&is_active&zone_id** — cross-user alert
  list, each row + owner `username`. - **GET /admin/alert-analytics** — `{total, active, inactive,
  triggered_ever, by_type[], by_sensor[], recently_triggered[]}`.

## Notes - The schema has **no per-alert trigger counter** (`last_triggered_at` is the first-ever
  fire for chart overlays), so distributions report by-type/by-sensor counts + last-7-days
  triggered, not a fabricated frequency. - Path is `/admin/alert-analytics` (not
  `/admin/alerts/analytics`) to avoid the greedy `/admin/alerts/{pk}` route.

## Verify 17 tests pass (`src/analytics/tests/test_admin_analytics.py` + existing overview/alerts) ·
  ruff + format clean · `manage.py check` clean.


## v1.48.0 (2026-06-19)

### Features

- **technician**: Scoped read-only RBAC backend (/technicians)
  ([#212](https://github.com/AgriLogy/agri-api/pull/212),
  [`cfb288d`](https://github.com/AgriLogy/agri-api/commit/cfb288d8facee211eff949c28a93d1f708661303))

Closes #211

## What Ships the owner-facing **technician management** backend the admin Technicians UI
  (agri-admin PR #10) is written against. Mounted at `/technicians` (JwtAuth): - `GET /technicians`
  → list (id, username, firstname, lastname, email, is_active, scope[{zone_id, allowed_graphs}]) -
  `POST /technicians` → create scoped login (username, password, names, email, scope) - `PUT
  /technicians/{id}/scope` → replace zone+graph scope - `POST /technicians/{id}/reset-password` →
  {status, password} - `DELETE /technicians/{id}` → revoke

A technician is a read-only login that sees **only granted zones**, and within a zone only the
  **granted ActiveGraph keys** (∩ what the owner enabled); it cannot mutate alerts/technicians/etc.
  Adds `TechnicianGrant` + `TechnicianZoneGrant` + `CustomUser.is_technician` and scope enforcement
  on the read/alert routers. Shape matches `agri-admin/lib/technicianApi.ts` exactly.

## How it was isolated The RBAC lived on the unpushed `feat/battery-signal-metrics` branch (commit
  3d77c6e), bundled with battery/signal sensor work. Cherry-picked **only** the technician RBAC; the
  dropped `api/__init__.py` conflict re-added a `/devices` mount (a separate unmerged feature) —
  excluded. A stray e2e assertion expecting `/users/me` to return email/phone (a different feature)
  was reverted.

## CI + prod-DB (the hard part) The two grant tables follow the proven **unmanaged template**
  (`managed=False` + FK `db_constraint=False`) — a real FK table broke the postgres CI flush before
  (the assistant model). They're created session-wide for tests in root `conftest.py`, and
  **self-deploy on prod** via `scripts/ensure_technician_tables.py` (creates both tables + the
  `is_technician` column idempotently on web boot, wired into `docker-entrypoint.sh`) — no manual
  migration. `is_technician` keeps its column-add migration for CI parity. The original `0064`
  migration (depended on a non-existent `0063_device`) was dropped.

## Verify - `pytest`: 8 technician tests + 126-test flush-heavy slice (users/alerts/assistant) green
  on sqlite; ruff lint + format clean; `manage.py check` clean. - The one failing e2e test
  (`test_workflow`) is **pre-existing & Postgres-only** (agri-core `AGRI_DB_URL` unset on sqlite) —
  fails identically on clean main; passes on CI Postgres. - After deploy: `ensure-technician-tables`
  line in boot logs; `/technicians` live via `/auth/sessions` JWT.


## v1.47.0 (2026-06-18)

### Features

- **assistant**: Irrigation advice + recent notifications tools
  ([#210](https://github.com/AgriLogy/agri-api/pull/210),
  [`a783261`](https://github.com/AgriLogy/agri-api/commit/a783261fa572519f859f6fe2a642dc1cf3746a70))

Closes #209

Final richer-assistant tool group (D+E).

## Tools - **`get_irrigation_advice`** (param `zone_id` optional → first zone): two-tier decision —
  agri-core's `field_snapshot(user)` Dr/RAW result when available, else a soil-moisture vs
  `critical_moisture_threshold` fallback. Always returns a complete payload (recommendation
  irrigate|hold|unknown, French reason, soil/et0/vpd, Dr/RAW, estimated water m³ + duration,
  decision_source). Read-only; wrapped in try/except so a missing agri-core DB never 500s. -
  **`list_recent_notifications`** (param `limit` default 5): recent `Notification` rows
  newest-first, composed into {id,title,message,date,type}.

## Orchestrator Rule-based routes `/irrigation` and `/notifications` (+ NL fr/en/ar).

## Verification `pytest src/apps/assistant` **72 passed** (10 new), `ruff` + `manage.py check`
  clean. No schema change. Units per spec: irrigation_water_quantity liters→m³ (÷1000),
  pomp_flow_rate L/s→m³/h (×3.6).


## v1.46.0 (2026-06-18)

### Features

- **assistant**: Sensor trend tool (get_sensor_trend)
  ([#208](https://github.com/AgriLogy/agri-api/pull/208),
  [`bf08004`](https://github.com/AgriLogy/agri-api/commit/bf080047c45e2cf691e353f94ba61c207163b3d7))

Closes #207

Adds `get_sensor_trend` — a rolling-window trend over any registered sensor key.

**Returns** `{key,label,unit,latest,min,max,avg,count,direction,window_start,window_end}`;
  `direction` is rising|falling|flat from the first-vs-last value in the window (flat unless both
  endpoints are non-null).

**Behaviour** - params: `sensor_key` (required, validated against SENSOR_SOURCES — unknown →
  `{error}`, never a 500), `zone_id` (optional), `hours` (optional, default 24). - scoped by `user`
  (+ `zone_id`); reads via the registry / `latest_reading`. - rule-based orchestrator routes
  `/trend` (+ NL fr/en/ar) with a `soilMoisture` default; LLM path fills `sensor_key`/`hours` via
  tool-calling. `_IntentRule` now carries default params.

**Tests:** `pytest src/apps/assistant` 62 passed (8 new: stats+rising, falling, flat,
  window-excludes-old, unknown-key-no-500, zone filter, user isolation, /trend chat route). ruff +
  manage.py check clean. No schema change.


## v1.45.0 (2026-06-18)

### Features

- **assistant**: Soil/plant/water snapshot tools
  ([#206](https://github.com/AgriLogy/agri-api/pull/206),
  [`828e5c6`](https://github.com/AgriLogy/agri-api/commit/828e5c6fc32829e84298ca283db77b863c1fcfbb))

Closes #205

Group B of the assistant's richer tools.

**New tools** (each accepts optional `zone_id`, returns the shared
  `{metrics:[{key,label,value,unit,status}]}` shape so the frontend reuses the FarmStatus metrics
  card): - `get_soil_status` — moisture & temperature at 3 depths, pH, salinity, conductivity, EC. -
  `get_plant_status` — leaf moisture/temperature, fruit size, large fruit diameter. -
  `get_water_status` — flow, pressure, EC, pH, precipitation rate, water level.

**Registry** — 20 new `SensorSource` entries (all `apps.sensors.models`, `app_label=analytics`);
  reads go through `latest_reading`, no raw ORM, no schema change.

**Orchestrator** — `/soil` `/plant` `/water` slash + NL routes (fr/en/ar) for the rule-based
  fallback; the LLM path picks the tools from the catalog.

Tests: 54 passed (catalog, per-domain metric retrieval, zone filter, user isolation, chat routing).
  ruff + `manage.py check` clean.


## v1.44.0 (2026-06-18)

### Features

- **assistant**: Zones tools (list_zones, get_zone_detail)
  ([#204](https://github.com/AgriLogy/agri-api/pull/204),
  [`55ba340`](https://github.com/AgriLogy/agri-api/commit/55ba340d99074eebdbd0333e03370e71a83d53e1))

## What

Adds two read-only tools to the AI assistant so users can ask about their irrigation zones in chat:

- **`list_zones`** — the caller's zones (name, `area_m2`, `critical_moisture`, soil params
  `TAW/FC/WP/RAW`), with an optional case-insensitive `zone_name` substring filter. -
  **`get_zone_detail`** — full details of one zone (soil params + `pomp_flow_rate` +
  `irrigation_water_quantity`), resolved by `zone_id` **or** `zone_name`; returns `{"zone": null}`
  when not found.

A new orchestrator rule routes `/zones` (and NL "my zones" / "mes zones" / "list zones" / Arabic
  مناطق) to `list_zones`.

## Notes

- Reuses the existing `Zone` model (`apps.irrigation.models`) — **no schema change**. - All data is
  scoped per-user through the tool registry (verified by an isolation test).

## Tests

`pytest src/apps/assistant/tests/test_assistant.py` → **40 passed**. Adds a `TestZonesTools` class
  (list, name filter, user isolation, detail-by-id, detail-by-name, missing→null, chat route) +
  orchestrator route params + catalog assertions. `ruff` clean.

Closes #203


## v1.43.0 (2026-06-18)

### Chores

- **assistant**: Tighten LLM system prompt (no function tags, use Markdown)
  ([#200](https://github.com/AgriLogy/agri-api/pull/200),
  [`ee9e077`](https://github.com/AgriLogy/agri-api/commit/ee9e0778a2c9181151825608f96c2d512b05dd9d))

Closes #199

The model occasionally emitted pseudo function-call syntax like `<function=...>` in its reply text.
  Tighten the system prompt: never mention tool/function names or emit call syntax/tags; format
  answers with simple **Markdown** (rendered on the frontend, companion agrilogy-front PR).

### Features

- **assistant**: Server-side conversation history
  ([#202](https://github.com/AgriLogy/agri-api/pull/202),
  [`84c5435`](https://github.com/AgriLogy/agri-api/commit/84c54355a66fc47035eb163a58ad84d027b2b6fc))

Closes #201

Persist the assistant's conversations per user, server-side, so history follows the account across
  devices.

- **`AssistantConversation`** — one row/conversation, messages as a JSON list; keyed by `(user,
  client_id)` (the frontend uuid) for stable identity across offline/online. - **Endpoints**
  (`/assistant`, JWT, user-scoped): `GET /conversations`, `PUT /conversations/{client_id}` (upsert),
  `DELETE /conversations/{client_id}`. - **Self-deploying table**: schema-of-record is agri-db and
  Django doesn't migrate on boot, so the table is created out-of-band by an idempotent
  `scripts/ensure_assistant_tables.py` run on web boot (the LoraUplink pattern) — no manual prod-DB
  migration. - 30 tests (history CRUD + user isolation); ruff + django check clean.

Frontend syncs to these endpoints (companion agrilogy-front PR).


## v1.42.1 (2026-06-18)

### Bug Fixes

- **assistant**: Send a User-Agent on LLM calls (Groq 403s urllib default)
  ([#198](https://github.com/AgriLogy/agri-api/pull/198),
  [`37d5b52`](https://github.com/AgriLogy/agri-api/commit/37d5b523127e9b36eef5027b7e1b45f835d5b9b5))

Closes #197

Groq's edge/WAF rejects stdlib urllib's default `User-Agent: Python-urllib/3.x` with **403**, so
  every LLM call silently fell back to rule-based. Verified in-container: default UA → 403, explicit
  UA → 200 (key, model, and tools all valid). Send `User-Agent: agri-api-assistant/1.0` in
  `llm._post`. 24 tests + ruff clean.


## v1.42.0 (2026-06-18)

### Continuous Integration

- Make backend auto-deploy single-line (zsh can't parse multi-line)
  ([#194](https://github.com/AgriLogy/agri-api/pull/194),
  [`0af481d`](https://github.com/AgriLogy/agri-api/commit/0af481db734558aeff373b968da7976d7b805860))

Closes #193

The appleboy SSH action runs the deploy `script` under the droplet's **zsh**, which fails to PARSE
  the multi-line `case`/`if` — the whole script aborted before `git reset` ran, so deploys silently
  never reached the droplet (every deploy this week was manual).

Reduce to **single-line commands only**: `fetch` + `reset --hard` + `restart web/worker/beat`. Drop
  the conditional build + `compose up` (rebuild needs `AGRI_DB_RO_TOKEN` not on the droplet;
  `compose up` conflicts with the externally-owned mailpit/redis). Code-only deploys just need a
  restart (bind-mounted code); dependency changes get a documented manual rebuild. Verified `bash
  -n` + `dash -n`.

Merging this triggers the **fixed** workflow on its own push → should be the first green
  auto-deploy.

### Features

- **assistant**: Provider-agnostic LLM orchestrator
  ([#196](https://github.com/AgriLogy/agri-api/pull/196),
  [`dc2c4a8`](https://github.com/AgriLogy/agri-api/commit/dc2c4a851bef2d12bdee476e229029880061a862))

Closes #195

The assistant can now **understand free-form questions** and call the right tool via an LLM, behind
  the existing `get_orchestrator()` seam (rule-based stays the fallback).

- **`llm.py`** — OpenAI-compatible tool-caller (stdlib `urllib`, no new dep). Feeds
  `registry.catalog()` to the model as tool schemas, runs the chosen tool through the same
  `run_tool` the HTTP routes use (data access stays behind the registry), feeds the result back,
  returns the model's natural-language reply + the tool data for the card. - **Provider-agnostic**:
  `AI_API_KEY` / `AI_API_BASE_URL` / `AI_MODEL` — defaults to **Groq free tier**
  (`llama-3.3-70b-versatile`). Not vendor-locked. - **Graceful fallback** to rule-based on any
  failure → always safe to deploy; the key just upgrades it. - `/assistant/chat` gains a `reply`
  field (model free text).

24 tests (incl. the agentic tool-call loop with a mocked API, fallback-on-error, schema conversion);
  ruff + django check clean.

**Activation**: set a free `AI_API_KEY` in the droplet `back/.env` + restart. Frontend consumes the
  new `reply` field in agrilogy-front (companion PR).


## v1.41.0 (2026-06-18)

### Continuous Integration

- Make deploy dep-check POSIX-robust (case glob, no pipefail/grep)
  ([#190](https://github.com/AgriLogy/agri-api/pull/190),
  [`4dc9e0b`](https://github.com/AgriLogy/agri-api/commit/4dc9e0b8a4b0d6a3f09c0f04e9e1645039e17c59))

Closes #189

The #188 conditional-build logic still failed the deploy run right after `git reset` — the `if git
  diff ... | grep -qE` pipeline (with `set -o pipefail` and a `\` line-continuation) misbehaves
  under the appleboy SSH action shell despite working locally.

## Change Replace it with a captured variable + POSIX `case` glob, and drop `pipefail`: ```sh
  CHANGED=$(git diff --name-only "$PREV" "$NEW") case "$CHANGED" in
  *back/uv.lock*|*back/pyproject.toml*) docker compose build ;; *) echo "No dependency change ->
  skipping build" ;; esac ``` Verified `bash -n` **and** `dash -n` clean; glob matches
  `back/uv.lock`/`back/pyproject.toml`, skips workflow-only changes.

Merging is workflow-only → the new deploy run should skip the build, `up -d`, restart the three app
  services, and finally go **green**.

- Only rebuild backend on dep changes; restart worker/beat after deploy
  ([#188](https://github.com/AgriLogy/agri-api/pull/188),
  [`af0c7f1`](https://github.com/AgriLogy/agri-api/commit/af0c7f1722503cb6eeacaffd8d3b794878317bdc))

Closes #187

The backend auto-deploy (`deploy-back.yml`) has been **failing on every push**, so deploys are
  currently all manual.

## Why it failed The script always ran `docker compose build`, which pulls the private `agri-db` dep
  and requires `AGRI_DB_RO_TOKEN` (missing on the droplet) → `could not read Username for
  github.com`. But the image bakes the venv and the app code is **bind-mounted** (`./back:/code`),
  so a code-only change needs **no build**.

Also, `docker compose up -d` does not recreate unchanged containers, so the Celery **worker/beat
  kept running stale code** (only the web hot-reloads via StatReloader) — which is why #180 and #186
  both needed a manual restart.

## Change - Rebuild **only** when `back/uv.lock` / `back/pyproject.toml` changed; skip otherwise →
  code-only deploys succeed without the token. - `docker compose restart agri-api-web
  agri-api-worker agri-api-beat` after `up -d` so bind-mounted code actually takes effect.

Merging this is itself a code-only (workflow) change → the new run will skip the build, `up -d`, and
  restart the three app services (brief blip), so it should be the first **green** auto-deploy.

## Still user-blocked (noted in #187, not fixed here) Dependency-change deploys still need a valid
  read-only `AGRI_DB_RO_TOKEN` for `AgriLogy/agri-db` on the droplet.

### Features

- **assistant**: Ai tool registry, HTTP routes & orchestrator
  ([#192](https://github.com/AgriLogy/agri-api/pull/192),
  [`ed328e9`](https://github.com/AgriLogy/agri-api/commit/ed328e95a24aa7c42a05a0e8478460b00c700c6d))

Closes #191

Backend the in-app assistant talks to — a senior, swappable **tool-calling** architecture. The
  assistant reads data **only** via HTTP tools (never the DB directly); an orchestrator maps the
  user message to the right tool.

## Layers (`apps/assistant`) - **`registry.py`** — DB abstraction: stable sensor keys → backing
  Django model + label/unit, one `latest_reading()` primitive. The only seam to the data layer. -
  **`tools.py`** — `ToolRegistry` of self-describing tools (name + description + param schema +
  handler): `get_sitemap`, `get_active_alerts`, `get_farm_status`, `get_weather`. Handlers read
  exclusively through the registry. - **`orchestrator.py`** — `Orchestrator` protocol +
  `RuleBasedOrchestrator` (slash + natural language, fr/en/ar → intent + tool + localized reply
  key). `get_orchestrator()` factory = drop-in seam for an LLM tool-caller (Claude tool-use over
  `registry.catalog()`).

## Routes (`/assistant`, JWT, user-scoped) - `GET /assistant/tools` — tool catalog - `POST
  /assistant/tools/{name}` — invoke one tool - `POST /assistant/chat` — understand → pick + run tool
  → `{intent, reply_key, tool, data}`

## Verification 18 tests (pure orchestrator routing + HTTP per intent + auth); `ruff check` +
  `manage.py check` clean.

Next: agri-front assistant calls `/assistant/chat` to replace mock data (separate PR); LLM
  orchestrator swap when a key is provisioned.


## v1.40.2 (2026-06-18)

### Bug Fixes

- **celery**: Isolate agri-api tasks onto a dedicated queue
  ([#186](https://github.com/AgriLogy/agri-api/pull/186),
  [`68b06e6`](https://github.com/AgriLogy/agri-api/commit/68b06e60b0451192bb554314cb852d5541965672))

Closes #185

The agri-api Celery worker shares a redis broker (`redis://redis:6379/0`) with the legacy
  **agriback** monolith stack, and both consumed the default `celery` queue. Consequences:

- agri-api worker rejects legacy `agriBack.tasks.*` as unregistered (KeyError noise every `*/2`). -
  **Our own scheduled tasks (`compute_et0`, `email_ping`) are silently dropped ~half the time** when
  they land on the legacy worker. Real reliability bug.

## Change - `CELERY_TASK_ROUTES = {"agriapi.*": {"queue": "agriapi"}}` (settings/base.py). - Worker
  started with `-Q agriapi` (docker-entrypoint.sh). - Routing tests (`test_celery_routing.py`).

Unrouted `agriBack.*` tasks stay on the default queue → the legacy worker keeps running the demo
  simulator (verified live on the droplet).

## Deploy note Reset `/root/agri-api` to main and restart **web + worker + beat together** (the
  entrypoint + settings are bind-mounted; no rebuild). Restarting all three avoids a window where
  the beat publishes to one queue while the worker listens on another.

## Verification Local: `app.amqp.router.route` sends `agriapi.tasks.*` → `agriapi` and
  `agriBack.tasks.*` → `celery`; routing tests pass; ruff clean.

### Continuous Integration

- Fix Auto Assign workflow failing on pull_request events
  ([#184](https://github.com/AgriLogy/agri-api/pull/184),
  [`2e49959`](https://github.com/AgriLogy/agri-api/commit/2e49959eadfb3ca6415d5f523ce58d8d99c366c7))

Closes #183

Replaces the broken `pozil/auto-assign-issue@v1` step (errors "Couldn't find issue info in current
  context" on `pull_request`, plus an invalid `numOfAssignee` input warning) with a single `gh api`
  call to the `issues/{number}/assignees` endpoint. That endpoint assigns **both** issues and PRs,
  so one step covers both triggers.

Same fix is being applied across all active org repos.


## v1.40.1 (2026-06-18)

### Bug Fixes

- **notifications**: Advance last_notified even when the send fails
  ([#180](https://github.com/AgriLogy/agri-api/pull/180),
  [`e4a1f54`](https://github.com/AgriLogy/agri-api/commit/e4a1f54040d11e0f7389b3fccc321c540a791523))

Closes #179

`send_periodic_notifications` advanced `last_notified` only after a **successful** `send_mail`, so a
  persistent provider failure (e.g. Resend `429 daily_quota_exceeded`) left every due user
  perpetually "due" and re-attempted them on **every** beat tick — hammering the provider. Observed
  live: `{sent:0, failed:7}` every 4 min.

**Fix:** move the `last_notified` update into a `finally` so it advances whether or not the send
  succeeds. A failed attempt counts as "notified for this cycle"; the next attempt waits for the
  user's cadence window instead of retrying every tick.

**Tradeoff:** a transiently-failed digest is skipped until the next cadence window rather than
  retried immediately — acceptable for a periodic field-status email, and far better than spamming
  the provider.

Adds `test_failed_send_still_advances_last_notified` (mocks `send_mail` to raise, asserts
  `last_notified` is advanced). `ruff check` clean.

### Chores

- **ci**: Auto-assign new issues and PRs to mks-zakaria
  ([#178](https://github.com/AgriLogy/agri-api/pull/178),
  [`916d071`](https://github.com/AgriLogy/agri-api/commit/916d071072a6b11feb141450c999b54a7d505ba5))

Closes #177

Adds `.github/workflows/auto-assign.yml` (`pozil/auto-assign-issue@v1`) so every newly opened issue
  and pull request is automatically assigned to `mks-zakaria`, enforcing the PR↔issue assignment
  convention without manual steps.

Takes effect once on `main`; future issues/PRs auto-assign on open.

### Continuous Integration

- Green the backend test suite (eager Celery + lora_uplink fixture)
  ([#182](https://github.com/AgriLogy/agri-api/pull/182),
  [`834188a`](https://github.com/AgriLogy/agri-api/commit/834188a91805625cd5ac000b4de783833ad7428c))

Closes #181

The CI **Backend tests** job runs pytest against Postgres in dev settings and has been red on `main`
  (and therefore every branch) on 8 environmental failures — no real code defects.

## Redis (5 tests) `TestZoneNotificationOutbound::*` dispatch `send_zone_outbound_email.delay()`
  (and SMS/WhatsApp). Dev settings aren't eager-Celery and CI provisions no redis service, so
  `.delay()` fails with `kombu ... Error -3 connecting to redis:6379`.

**Fix:** `CELERY_TASK_ALWAYS_EAGER` is now env-driven in `settings/base.py` (default **off** —
  dev/prod dispatch to the real worker unchanged). The CI pytest step sets
  `CELERY_TASK_ALWAYS_EAGER=True` so tasks run inline, no broker needed. The sqlite `test` settings
  still force it on explicitly.

## lora_uplink (3 tests) `LoraUplink` is intentionally **not** migrated (the model docstring: prod
  creates it out-of-band via `schema_editor().create_model()`), so pytest-django's migration-built
  test DB lacks the table.

**Fix:** a session-scoped fixture in a new `apps/lorawan/chirpstack/conftest.py` creates
  `lora_uplink` the same way prod does, once per session — keeping the not-migrated contract intact.

## Verification Ran the 8 previously-failing tests under dev settings locally: **28 passed, 6
  skipped** (the 6 are the Postgres-only dual-ORM handler tests). Ruff check + format clean. Final
  Postgres confirmation is this PR's own CI run.

Unblocks clean CI for all PRs, notably #180.


## v1.40.0 (2026-06-17)

### Features

- **notifications**: Deliver zone-outbound over SMS + WhatsApp (Twilio)
  ([#176](https://github.com/AgriLogy/agri-api/pull/176),
  [`7d83262`](https://github.com/AgriLogy/agri-api/commit/7d832621efb24cbae4fd502ddd322742de99f60c))

Closes #174

Re-opened against main (the original #175 auto-closed when its base branch
  `fix/zone-outbound-async-email` was deleted on merge of #171). Now that #171 is in main, this diff
  is just the SMS/WhatsApp delta.

## What - **`agriapi.twilio_messaging`** — stdlib-only `send_sms`/`send_whatsapp` (Twilio Messages
  API, Basic auth, E.164 normalisation), reads `TWILIO_*` from env. - **`send_zone_outbound_sms` /
  `send_zone_outbound_whatsapp`** Celery tasks. - **zone-outbound** accepts `contactPhone`, fans out
  to every enabled channel → `{status: queued, channels: [...]}`; 400 only when no channel has a
  usable recipient. - 13 tests; ruff clean.

## Deploy Set `TWILIO_ACCOUNT_SID/AUTH_TOKEN/SMS_FROM/WHATSAPP_FROM` on the droplet `.env`. Overlaps
  PR #163's `whatsapp.py` — consolidate later.


## v1.39.1 (2026-06-17)

### Bug Fixes

- **email**: Deliver via Resend HTTP API instead of blocked SMTP
  ([#171](https://github.com/AgriLogy/agri-api/pull/171),
  [`11164ef`](https://github.com/AgriLogy/agri-api/commit/11164efb84a913470c17c9e6413e6e2e384b5dc9))

Closes #170

## Why The droplet cannot make outbound SMTP connections (DigitalOcean blocks 25/465/587; 443 is
  open). `send_mail` blocks on connect until nginx 504s, so notification emails never deliver — both
  the periodic emails and the `/notifications/zone-outbound` custom-email send.

## What - **New `agriapi.email_backends.ResendEmailBackend`** — stdlib-only (urllib) sender over
  Resend's HTTPS API; drop-in for every `send_mail`/`EmailMessage`. Default `EMAIL_BACKEND` in prod;
  selectable via env in dev. - **`EMAIL_TIMEOUT=10`** in base settings — no send can hang
  requests/workers ~60s again. - **`RESEND_API_KEY`** read in base settings (applies under both dev
  and prod modules; the deployed container runs dev settings via `DJANGO_ENV`). - **`zone-outbound`
  now enqueues** `send_zone_outbound_email` (Celery) instead of sending inline → returns `202
  queued`, request can't block. - SMTP env vars required only when the smtp backend is explicitly
  selected.

## Tests - New `test_resend_backend.py` (payload shape, HTML alt, timeout, missing-key behaviour). -
  Updated `zone-outbound` assertions to 202/queued. - `15 passed, 6 skipped` (Postgres-only skips);
  ruff clean.

## Deploy notes Set `RESEND_API_KEY` and `EMAIL_BACKEND=agriapi.email_backends.ResendEmailBackend`
  in `back/.env`. No rebuild needed (stdlib-only; code is bind-mounted). Requires a verified Resend
  sending domain for `noreply@agrogo-datafarm.com`.


## v1.39.0 (2026-06-17)

### Features

- **notifications**: Minute-based notify_every cadence + faster email beat
  ([#173](https://github.com/AgriLogy/agri-api/pull/173),
  [`9bae9f0`](https://github.com/AgriLogy/agri-api/commit/9bae9f02e0c15925af04304d687ca916b9176aaa))

Closes #172

## What Reinterprets `notify_every` as **minutes** so sub-hour cadences work. - `should_notify`:
  `*60` (was `*3600`), default 240 (=4h). - Admin validation 10..10080 min (was 1..168 h). - Prod
  email beat `crontab(minute=0)` -> `*/5` so a 10-min cadence is honoured (per-user gate unchanged).
  - Model default/help_text + task docstring -> minutes. Tests updated, new sub-hour test (19 pass,
  ruff clean).

## ⚠️ Coordinated deploy (do NOT merge alone) Needs the **agri-db backfill** (`UPDATE …
  notify_every*60`) applied **first**, then this, then the **agri-admin** minutes UI. Merging this
  alone (without backfill) would make existing rows mean 'every 4 min'. Migration lineage + backfill
  pending a prod Alembic-head check.


## v1.38.0 (2026-06-12)

### Chores

- **deps**: Bump agri-core to 0.13.0 (registers vpd sensor key)
  ([#167](https://github.com/AgriLogy/agri-api/pull/167),
  [`155892d`](https://github.com/AgriLogy/agri-api/commit/155892d5b606a33717c368d01f602ec921ea1b0b))

Closes #166

agri-core 0.13.0 (AgriLogy/agri-core#30) adds `vpd` to `SENSOR_KEY_REGISTRY`. With this bump, the
  DPV/VPD card's *Create alert* drawer can suggest, create, and evaluate alerts against the
  already-populated `VPDWeather` rows (written by the ET₀ calc task) — no migration or new data
  pipeline needed.

- `back/pyproject.toml`: agri-core `0.12.0` → `0.13.0` - `back/uv.lock`: refreshed (`uv lock`)

Frontend counterpart (the missing red button): AgriLogy/agrilogy-front#138.

### Features

- **sensors**: Expose VPDWeather as /sensors/vpdweather
  ([#169](https://github.com/AgriLogy/agri-api/pull/169),
  [`2286a02`](https://github.com/AgriLogy/agri-api/commit/2286a02176c73f7e3a640f24c77c8073393deb15))

Closes #168

VPD is computed hourly by `compute_et0_vpd_hourly` (alongside ET₀) and stored in `VPDWeather`, but
  had no read endpoint — so the DPV chart re-derived it client-side by joining humidity+temperature
  on exact timestamps, which yields nothing when readings arrive on separate uplinks
  (Bivocom/LoRaWAN). This registers the model in `SENSOR_MODELS`, auto-generating `GET
  /sensors/vpdweather` (hourly-averaged, kPa) — matching how ET₀ serves its calculated series, and
  consistent with what the alert engine evaluates.

Verified locally: `VPDWeather in SENSOR_MODELS == True` and the route `/sensors/vpdweather` is
  registered on the NinjaAPI. ruff clean.

Frontend counterpart (fetch this series instead of the client-side join): agrilogy-front PR to
  follow.


## v1.37.1 (2026-06-12)

### Bug Fixes

- **users**: Default payement_status on admin create
  ([#165](https://github.com/AgriLogy/agri-api/pull/165),
  [`f28fc76`](https://github.com/AgriLogy/agri-api/commit/f28fc76afcbde3cc0f202161dc63ab7a635b593b))

Closes #164


## v1.37.0 (2026-06-09)

### Features

- **sensors**: Delegate hourly aggregation to agri-core
  ([#157](https://github.com/AgriLogy/agri-api/pull/157),
  [`4a4eaaf`](https://github.com/AgriLogy/agri-api/commit/4a4eaafbab084d01939064189624e95c3ef6de46))

Closes #156


## v1.36.0 (2026-06-09)

### Features

- **sensors**: Aggregate readings to one averaged value per hour
  ([#155](https://github.com/AgriLogy/agri-api/pull/155),
  [`1cd5078`](https://github.com/AgriLogy/agri-api/commit/1cd5078fa9d05a83c2f36296b6d4ba73c38f1713))

Closes #154


## v1.35.0 (2026-06-07)

### Features

- **lorawan**: Graph battery + signal as per-zone metrics
  ([#153](https://github.com/AgriLogy/agri-api/pull/153),
  [`0d8d6f8`](https://github.com/AgriLogy/agri-api/commit/0d8d6f8b1ed5061d086c0aa188d9f1b49eff2e22))

Closes #152

Promotes **battery** (V) and **signal/RSSI** (dBm) to first-class per-zone sensor metrics — the
  shared dashboard renders them for LoRa *or* Bivocom whenever a device reports them (no per-device
  branching).

- Django `BatterySensor`/`SignalSensor` models + auto-registered `/api/sensors/battery`,`/signal`
  read routes (via `SENSOR_MODELS`) - ChirpStack ingest writes **pH + battery + signal** each uplink
  and dispatches alerts per metric → enables the **low-battery alert** - agri-core pin → `0.10.0`
  (battery/signal registry keys, LESS_THAN condition) → transitively agri-db `0.2.0`

**Note on the migration:** the analytics app's *test* DB is migration-built, so a Django migration
  (`0060`) is required for the new tables; the **agri-db Alembic migration remains the
  schema-of-record**, and prod tables are created out-of-band (Django schema editor) like the
  existing analytics tables. Completes the cascade: agri-db ✓ → agri-core ✓ → **agri-api**.


## v1.34.0 (2026-06-07)

### Features

- **lorawan**: Store full ChirpStack uplinks (battery + all fields)
  ([#151](https://github.com/AgriLogy/agri-api/pull/151),
  [`f4501c3`](https://github.com/AgriLogy/agri-api/commit/f4501c389ec59d71fc4af882fe1a5b4d685898fe))

Closes #150

Adds a `LoraUplink` table that captures **every** uplink in full so no device data is dropped: -
  **battery_v** (BatV on data frames / BAT on status frames) — promoted to its own column as the
  most operationally-critical metric - pH, RSSI, SNR, fCnt, fPort, frequency - the complete
  codec-decoded `object` (JSON) + the raw base64 payload

Every frame is now persisted (status/battery-only frames included). The per-metric `PhSoil` write
  still happens for pH frames, so the dashboard pH graph is unchanged. No agri-api migration — the
  table is created out-of-band via the schema editor (matching how the analytics tables already
  work). Adds `txInfo.frequency` to the schema and tests for battery decode + full-record
  persistence.

(Includes a tiny `chore` commit syncing uv.lock to the released version 1.33.0 — it was drifted on
  main.)


## v1.33.0 (2026-06-05)

### Features

- **lorawan**: Persist ChirpStack RS485-LB pH uplinks into a lora zone
  ([#149](https://github.com/AgriLogy/agri-api/pull/149),
  [`a86044e`](https://github.com/AgriLogy/agri-api/commit/a86044eec798941605d94ee8abf2bcea0db3f760))

Closes #148

Completes the ChirpStack v4 webhook (was a Phase-6.5 stub that discarded the payload): - **Decode
  pH** from the codec `object` if present, else straight from the raw RS485-LB bytes (`bytes[3..4] /
  100`, e.g. `0x02F6`=758=**7.58** — verified against a real captured frame). Status frames (fPort
  5) and out-of-range values are accepted (202) but not stored. - **Persist** the reading as a
  `PhSoil` row under a dedicated, lazily-provisioned `lora` zone (owner = a `lora` user) — grouping
  every LoRaWAN device under one zone so it shows on the dashboard pH graph, reusing the live
  weather-ingest write pattern. - **No schema change** (rows only; schema-of-record stays in
  agri-db).

Adds the raw `data` field to the uplink schema + **11 tests** (decode vectors + endpoint persistence
  into the `lora` zone). Decoder defaults to **soil** pH; trivial switch to water pH if needed.


## v1.32.0 (2026-06-01)

### Continuous Integration

- Add a pytest-cov coverage gate (fail_under=85)
  ([#145](https://github.com/AgriLogy/agri-api/pull/145),
  [`854afd5`](https://github.com/AgriLogy/agri-api/commit/854afd5289b68528b1fa523dc534cc3ba001db27))

Closes #22.

### Features

- **prod**: Production-harden agri-api serving (gunicorn + whitenoise)
  ([#147](https://github.com/AgriLogy/agri-api/pull/147),
  [`c6a4546`](https://github.com/AgriLogy/agri-api/commit/c6a4546867e30c58cb03441efaa0a1b187ec3de6))

Closes #146

## What Make agri-api production-servable so it can deploy to `back.agrogo-datafarm.com` (the REST
  backend the post-#70 frontend targets).

- **deps**: add `gunicorn==23.0.0` + `whitenoise==6.8.2` (uv.lock regenerated). -
  **settings/base.py**: `WhiteNoiseMiddleware` after `SecurityMiddleware`; `STORAGES` →
  `whitenoise.storage.CompressedStaticFilesStorage`. - **docker-entrypoint.sh** (`web` role): gated
  on `DJANGO_ENV` — `prod` runs `collectstatic` + `gunicorn agriapi.wsgi:application` (no dev
  seeders); non-prod unchanged (`runserver` + seeders).

`SECURE_PROXY_SSL_HEADER` is already set in base.py, so this works behind the nginx TLS terminator
  without a redirect loop.

## Validation - `DJANGO_ENV=dev manage.py check` → no issues. - `collectstatic --dry-run` → 200
  files via whitenoise storage. - `bash -n docker-entrypoint.sh` → clean.

## Notes Dev workflow unchanged (still `runserver` + seeders). Part of the agri-api deployment plan
  (Supabase prod DB).


## v1.31.2 (2026-05-30)

### Bug Fixes

- **et0**: Make compute_et0_vpd_hourly idempotent per (zone, timestamp)
  ([#144](https://github.com/AgriLogy/agri-api/pull/144),
  [`5f2261c`](https://github.com/AgriLogy/agri-api/commit/5f2261c67b36bc2b665d7c4d9bc6688dfa7f5fb6))

Closes #16.

### Continuous Integration

- Run the full src/ test suite so e2e + ingest tests gate merges
  ([#142](https://github.com/AgriLogy/agri-api/pull/142),
  [`d5efc3f`](https://github.com/AgriLogy/agri-api/commit/d5efc3f84adcd6fa8e89c5968f4304f50fc596fd))

Closes #141.

### Refactoring

- **analytics**: Relocate routers + alert engine into domain apps
  ([#138](https://github.com/AgriLogy/agri-api/pull/138),
  [`d2b25ec`](https://github.com/AgriLogy/agri-api/commit/d2b25ec889373ea6576012f25433985ef97512c6))

Closes #137.

- **analytics**: Split the god-app into apps/irrigation + apps/alerts
  ([#134](https://github.com/AgriLogy/agri-api/pull/134),
  [`086e18c`](https://github.com/AgriLogy/agri-api/commit/086e18c1ff33cb385b335a8dad12de539374ddbd))

Closes #133.

- **et0**: Thin the Celery ET0 adapter onto agri-core's fetch-and-compute
  ([#132](https://github.com/AgriLogy/agri-api/pull/132),
  [`d852d3e`](https://github.com/AgriLogy/agri-api/commit/d852d3e4487d7f8fc6e17281113aed3a0536247b))

Closes #131.

- **layout**: Adopt src/ layout — move agriapi/apps/analytics under back/src/
  ([#140](https://github.com/AgriLogy/agri-api/pull/140),
  [`3f328da`](https://github.com/AgriLogy/agri-api/commit/3f328da3fde8c499b377ddaf402c056c772df569))

Closes #139.

- **project**: Rename the Django project package agriBack → agriapi
  ([#136](https://github.com/AgriLogy/agri-api/pull/136),
  [`ca0586a`](https://github.com/AgriLogy/agri-api/commit/ca0586a840dd0c3c30b1f9e996eb49d2635511b0))

Closes #135.

- **users**: Drop the dead legacy DRF auth serializers
  ([#143](https://github.com/AgriLogy/agri-api/pull/143),
  [`036d442`](https://github.com/AgriLogy/agri-api/commit/036d44249271d1c4589ff2179f572284998afd7f))

Closes #23.


## v1.31.1 (2026-05-30)

### Bug Fixes

- **scheduler**: Make the sensor simulator opt-in so servers use real data
  ([#130](https://github.com/AgriLogy/agri-api/pull/130),
  [`2668aa7`](https://github.com/AgriLogy/agri-api/commit/2668aa74c179458f26a2b848e9671b81a852d6c0))

Closes #129.


## v1.31.0 (2026-05-30)

### Features

- Wire alerts + notification adapters to agri-core (dual-ORM)
  ([#128](https://github.com/AgriLogy/agri-api/pull/128),
  [`c7f0de9`](https://github.com/AgriLogy/agri-api/commit/c7f0de91b6448f010f2652d010f36c394f401baf))

Completes the dual-ORM activation. Closes #127.


## v1.30.0 (2026-05-30)

### Features

- **agronomy**: Wire field_snapshot to agri-core's DB-backed handler
  ([#126](https://github.com/AgriLogy/agri-api/pull/126),
  [`05a8b50`](https://github.com/AgriLogy/agri-api/commit/05a8b50601a8325c8200ed71cb47f4cd398a61cd))

Dual-ORM activation, first slice. Closes #125.


## v1.29.4 (2026-05-30)

### Bug Fixes

- **deps**: Pin agri-core@0.6.1 + pre-push gate + deflake field_snapshot test
  ([#124](https://github.com/AgriLogy/agri-api/pull/124),
  [`8a25b94`](https://github.com/AgriLogy/agri-api/commit/8a25b94319ffe49045c79326d9c547cef631033c))

Completes the pydantic cleanup cascade; adds pre-push gate; deflakes the date-sensitive
  field_snapshot et0 test. Closes #123.


## v1.29.3 (2026-05-29)

### Bug Fixes

- **alerts**: Restore sensor_key + zone-ownership validation on alert writes
  ([#122](https://github.com/AgriLogy/agri-api/pull/122),
  [`788183a`](https://github.com/AgriLogy/agri-api/commit/788183a8da9115abc91528cf6cb5d3d23ae32851))

Re-add the sensor_key-registry + zone-ownership checks dropped in the #116 ninja migration, on
  create/replace/patch. Closes #121.

### Chores

- **release**: Migrate agri-api to python-semantic-release
  ([#120](https://github.com/AgriLogy/agri-api/pull/120),
  [`c25e2db`](https://github.com/AgriLogy/agri-api/commit/c25e2db7a2074b08624f06de97d256f6ad21e3ca))

Swap JS semantic-release for python-semantic-release; author pinned to mks-zakaria; v{version} tags
  continue. Closes #119.


## v1.29.2 (2026-05-29)

### Chores

- **deps**: Pin agri-core@0.6.0 and migrate tests to django-ninja
  ([#118](https://github.com/AgriLogy/agri-api/pull/118),
  [`b33b095`](https://github.com/AgriLogy/agri-api/commit/b33b095eab82e56cd324b982444e31b1bd62498a))

Pins agri-core@0.6.0 (→ agri-db@0.1.0), wires AGRI_DB_RO_TOKEN for the private fetch, bumps uv to
  0.11.6, and migrates the orphaned DRF tests to django-ninja (fixes #116). CI: 213 passed.

- **release**: 1.29.2 [skip ci]
  ([`acfe5ff`](https://github.com/AgriLogy/agri-api/commit/acfe5ff932fd5a49928802c71083726907351c69))

## [1.29.2](https://github.com/AgriLogy/agri-api/compare/v1.29.1...v1.29.2) (2026-05-29)


## v1.29.1 (2026-05-28)

### Chores

- **release**: 1.29.1 [skip ci]
  ([`70cee51`](https://github.com/AgriLogy/agri-api/commit/70cee517d31037afc71816f949dd7a0ba9e4c691))

## [1.29.1](https://github.com/AgriLogy/agri-api/compare/v1.29.0...v1.29.1) (2026-05-28)

### Refactoring

- **api**: Unify URLs to REST-standard scheme
  ([#116](https://github.com/AgriLogy/agri-api/pull/116),
  [`4c412bf`](https://github.com/AgriLogy/agri-api/commit/4c412bf89a03453d0b33add86f1d2d7e58f8456d))

Closes #115.

Atomic URL rewrite. 14/14 tests pass; live smoke green against the running stack. Frontend repo will
  need a coordinated client update (mapping in the issue).


## v1.29.0 (2026-05-28)

### Chores

- **release**: 1.29.0 [skip ci]
  ([`7f1c29e`](https://github.com/AgriLogy/agri-api/commit/7f1c29e4b1dc416f7607ab4deb8771f97bf99156))

# [1.29.0](https://github.com/AgriLogy/agri-api/compare/v1.28.0...v1.29.0) (2026-05-28)

### Features

* **api:** migrate the admin tree to django-ninja (PR 10/10)
  ([#114](https://github.com/AgriLogy/agri-api/issues/114))
  ([45308e4](https://github.com/AgriLogy/agri-api/commit/45308e418dc83e2f59430165e38432ff6de6ddea))

### Features

- **api**: Migrate the admin tree to django-ninja (PR 10/10)
  ([#114](https://github.com/AgriLogy/agri-api/pull/114),
  [`45308e4`](https://github.com/AgriLogy/agri-api/commit/45308e418dc83e2f59430165e38432ff6de6ddea))

Final PR in the DRF -> django-ninja sweep. ~14 admin views migrated; legacy active-graph URL
  retained. 14/14 tests pass. Progress: **10/10**.


## v1.28.0 (2026-05-28)

### Chores

- **release**: 1.28.0 [skip ci]
  ([`94e9bee`](https://github.com/AgriLogy/agri-api/commit/94e9bee0868f5269fc8c5207f5a2eda0e869a5d9))

# [1.28.0](https://github.com/AgriLogy/agri-api/compare/v1.27.0...v1.28.0) (2026-05-28)

### Features

* **api:** migrate weather ingest webhook to django-ninja
  ([#112](https://github.com/AgriLogy/agri-api/issues/112))
  ([82396ac](https://github.com/AgriLogy/agri-api/commit/82396ac06bf701f52ad8e8f0ca047c5193a537a1))

### Features

- **api**: Migrate weather ingest webhook to django-ninja
  ([#112](https://github.com/AgriLogy/agri-api/pull/112),
  [`82396ac`](https://github.com/AgriLogy/agri-api/commit/82396ac06bf701f52ad8e8f0ca047c5193a537a1))

Multi-sensor ingest endpoint migrated; e2e workflow extended with chapter 14. 14/14 tests pass.
  Progress: 8/10.


## v1.27.0 (2026-05-28)

### Chores

- **release**: 1.27.0 [skip ci]
  ([`0df640e`](https://github.com/AgriLogy/agri-api/commit/0df640e605235608d18787a5a773d4c47e3ba223))

# [1.27.0](https://github.com/AgriLogy/agri-api/compare/v1.26.0...v1.27.0) (2026-05-28)

### Features

* **api:** migrate auto-generated sensor routes to django-ninja + bootstrap e2e workflow test
  ([#110](https://github.com/AgriLogy/agri-api/issues/110))
  ([bc8a6fb](https://github.com/AgriLogy/agri-api/commit/bc8a6fbd7f25379c6ec1db9f70c2405046a5e340))

### Features

- **api**: Migrate auto-generated sensor routes to django-ninja + bootstrap e2e workflow test
  ([#110](https://github.com/AgriLogy/agri-api/pull/110),
  [`bc8a6fb`](https://github.com/AgriLogy/agri-api/commit/bc8a6fbd7f25379c6ec1db9f70c2405046a5e340))

Closes the auto-sensor-routes issue. 34 GET/PATCH pairs migrated; new e2e workflow test bootstrapped
  (13 chapters). 14/14 unit + e2e tests pass. Progress: 7/10.


## v1.26.0 (2026-05-28)

### Chores

- **release**: 1.26.0 [skip ci]
  ([`325b028`](https://github.com/AgriLogy/agri-api/commit/325b02864a9bae51fb09ffcda0b99e907cfbabef))

# [1.26.0](https://github.com/AgriLogy/agri-api/compare/v1.25.0...v1.26.0) (2026-05-28)

### Features

* **api:** migrate manager-affirmation endpoints to django-ninja
  ([#108](https://github.com/AgriLogy/agri-api/issues/108))
  ([2d709bc](https://github.com/AgriLogy/agri-api/commit/2d709bc290523c2ae42788d1bd08c77047d2b43e))

### Features

- **api**: Migrate manager-affirmation endpoints to django-ninja
  ([#108](https://github.com/AgriLogy/agri-api/pull/108),
  [`2d709bc`](https://github.com/AgriLogy/agri-api/commit/2d709bc290523c2ae42788d1bd08c77047d2b43e))

Closes #107. 2 endpoints migrated under /api/. Smoke green across own-list (200), create (201),
  bogus action (400), non-admin decide (403). Progress: 9/10.


## v1.25.0 (2026-05-28)

### Chores

- **release**: 1.25.0 [skip ci]
  ([`ae64ee5`](https://github.com/AgriLogy/agri-api/commit/ae64ee58622ca48a392c42db224f6ac337059e15))

# [1.25.0](https://github.com/AgriLogy/agri-api/compare/v1.24.0...v1.25.0) (2026-05-28)

### Features

* **api:** migrate notifications endpoints to django-ninja
  ([#106](https://github.com/AgriLogy/agri-api/issues/106))
  ([8f03c22](https://github.com/AgriLogy/agri-api/commit/8f03c22127ba825abef156b4951285cdc8ca6326))

### Features

- **api**: Migrate notifications endpoints to django-ninja
  ([#106](https://github.com/AgriLogy/agri-api/pull/106),
  [`8f03c22`](https://github.com/AgriLogy/agri-api/commit/8f03c22127ba825abef156b4951285cdc8ca6326))

Closes the notifications-migration issue. 2 endpoints migrated. Smoke green. Progress: 6/10.
  **Stopping the migration sweep here** — PRs 7–10 (auto-sensor routes, weather ingest, manager
  affirmation, admin tree) deferred to a future scoped session.


## v1.24.0 (2026-05-28)

### Chores

- **release**: 1.24.0 [skip ci]
  ([`48693a8`](https://github.com/AgriLogy/agri-api/commit/48693a8c6ddea4a86db7c5a0770e789c438abbb6))

# [1.24.0](https://github.com/AgriLogy/agri-api/compare/v1.23.0...v1.24.0) (2026-05-28)

### Features

* **api:** migrate alerts CRUD to django-ninja
  ([#104](https://github.com/AgriLogy/agri-api/issues/104))
  ([ee1d152](https://github.com/AgriLogy/agri-api/commit/ee1d152453d2df271b3590e4f72ece5ea263ffda))

### Features

- **api**: Migrate alerts CRUD to django-ninja
  ([#104](https://github.com/AgriLogy/agri-api/pull/104),
  [`ee1d152`](https://github.com/AgriLogy/agri-api/commit/ee1d152453d2df271b3590e4f72ece5ea263ffda))

Closes the alerts-CRUD migration issue. Note: pre-existing DB schema gap (migration
  0059_alert_last_emailed_at unapplied) breaks the Alert queryset on list/for-graph; unrelated to
  this PR. Smoke green on sensor-keys (200, 35 keys) and suggest (200).

Progress: 5/10.


## v1.23.0 (2026-05-28)

### Chores

- **release**: 1.23.0 [skip ci]
  ([`fafabf9`](https://github.com/AgriLogy/agri-api/commit/fafabf9dd07639f2abbd43c9e577c37063049742))

# [1.23.0](https://github.com/AgriLogy/agri-api/compare/v1.22.0...v1.23.0) (2026-05-28)

### Features

* **api:** migrate analytics read endpoints to django-ninja
  ([#102](https://github.com/AgriLogy/agri-api/issues/102))
  ([bd2aec0](https://github.com/AgriLogy/agri-api/commit/bd2aec074994f2a0ad78959eb23d54634118a62a))

### Features

- **api**: Migrate analytics read endpoints to django-ninja
  ([#102](https://github.com/AgriLogy/agri-api/pull/102),
  [`bd2aec0`](https://github.com/AgriLogy/agri-api/commit/bd2aec074994f2a0ad78959eb23d54634118a62a))

Closes #101. 4 reads migrated under /api/. Live smoke green: header 200, zones-names-per-user 200,
  active-graph/self/1 200, non-staff active-zones 403.

Progress: 4/10.


## v1.22.0 (2026-05-28)

### Chores

- **release**: 1.22.0 [skip ci]
  ([`631d7d1`](https://github.com/AgriLogy/agri-api/commit/631d7d18cc00a3f1f7d65624e38a82026b5be67a))

# [1.22.0](https://github.com/AgriLogy/agri-api/compare/v1.21.0...v1.22.0) (2026-05-28)

### Features

* **api:** migrate apps/users auth endpoints to django-ninja
  ([#100](https://github.com/AgriLogy/agri-api/issues/100))
  ([a53b690](https://github.com/AgriLogy/agri-api/commit/a53b690f020a9e09f6de820ed47de6a512823a6b))

### Features

- **api**: Migrate apps/users auth endpoints to django-ninja
  ([#100](https://github.com/AgriLogy/agri-api/pull/100),
  [`a53b690`](https://github.com/AgriLogy/agri-api/commit/a53b690f020a9e09f6de820ed47de6a512823a6b))

Closes #99. 7 routes migrated under /auth/. Admin sub-tree (/auth/admin/) and simplejwt token routes
  stay DRF for now. Live smoke green across signin (200), admin-signin (200, no is_staff), bad creds
  (401), duplicate signup (400), non-staff admin endpoint (403), send-notification (200 with mailpit
  delivery), and legacy /auth/token/ (200).

Progress: 3/10.


## v1.21.0 (2026-05-28)

### Chores

- **release**: 1.21.0 [skip ci]
  ([`3c190f1`](https://github.com/AgriLogy/agri-api/commit/3c190f16b623cea33598d509577d075087cf9fa0))

# [1.21.0](https://github.com/AgriLogy/agri-api/compare/v1.20.0...v1.21.0) (2026-05-28)

### Features

* **api:** migrate Bivocom uplink webhook to django-ninja
  ([#98](https://github.com/AgriLogy/agri-api/issues/98))
  ([2cb2d8e](https://github.com/AgriLogy/agri-api/commit/2cb2d8e977e7754ccc0160dbd1d5367f1555f1d3)),
  closes [#96](https://github.com/AgriLogy/agri-api/issues/96)

### Features

- **api**: Migrate Bivocom uplink webhook to django-ninja
  ([#98](https://github.com/AgriLogy/agri-api/pull/98),
  [`2cb2d8e`](https://github.com/AgriLogy/agri-api/commit/2cb2d8e977e7754ccc0160dbd1d5367f1555f1d3))

Closes #97. See #96 for the same pattern applied to ChirpStack — this PR mirrors it for Bivocom.

11/11 pytest pass across apps/bivocom and apps/lorawan/chirpstack; live smoke clean (202 valid, 422
  invalid).

Progress: 2/10.


## v1.20.0 (2026-05-28)

### Chores

- **release**: 1.20.0 [skip ci]
  ([`94e8561`](https://github.com/AgriLogy/agri-api/commit/94e8561b533a931560ae9868999572a6ab26fd1f))

# [1.20.0](https://github.com/AgriLogy/agri-api/compare/v1.19.0...v1.20.0) (2026-05-28)

### Features

* **api:** migrate ChirpStack uplink webhook to django-ninja
  ([#96](https://github.com/AgriLogy/agri-api/issues/96))
  ([76b8b74](https://github.com/AgriLogy/agri-api/commit/76b8b740698a72ee532d4e6a58cf1bef8ab185d7))

### Features

- **api**: Migrate ChirpStack uplink webhook to django-ninja
  ([#96](https://github.com/AgriLogy/agri-api/pull/96),
  [`76b8b74`](https://github.com/AgriLogy/agri-api/commit/76b8b740698a72ee532d4e6a58cf1bef8ab185d7))

Closes #95.

Migration 1 of 10 in the DRF → django-ninja sweep.

## What changes

- Drop `apps/lorawan/chirpstack/views.py` (DRF APIView) + `urls.py`. - Add
  `apps/lorawan/chirpstack/router.py` (ninja Router, same path, same payload schemas, same response
  shape). - **Restructure NinjaAPI mount**: now mounted at the URL root with each router carrying
  its full path. Lets us migrate any path in place. PR #94's `/api/v2/sensors/keys` still works; new
  chirpstack route stays at `/api/v1/lorawan/chirpstack/uplink`. - Drop
  `path("api/v1/lorawan/chirpstack/", include(...))` from `agriBack/urls.py`. - Route opts out of
  JWT auth (`auth=None`) — gateway uses a shared-secret header today.

## Behavior changes

- Validation errors now use django-ninja's **422** + `{"detail": [...]}` envelope (was DRF-side 400
  + custom shape). ChirpStack treats any 4xx as "don't retry", so the gateway behavior is unchanged.
  Frontend doesn't call this endpoint. - 202 happy-path response shape is **identical**
  (`{"accepted", "devEui", "channels"}`). - `test_uplink_endpoint_rejects_invalid` updated to assert
  422 + `detail`.

## Verification

- `manage.py check` clean. - `uv run pytest apps/lorawan/chirpstack/tests.py` — 6/6. - Live smoke:
  202 valid + 422 invalid. - v2 untouched: `/api/v2/sensors/keys` 401/200 paths still green (2/2 in
  `agriBack.api.tests`). - Django `/admin/login/` 200 (legacy includes still work via fall-through).


## v1.19.0 (2026-05-28)

### Chores

- **release**: 1.19.0 [skip ci]
  ([`b5201ff`](https://github.com/AgriLogy/agri-api/commit/b5201ffb5e09a4b6c2cab469072472d3931baa42))

# [1.19.0](https://github.com/AgriLogy/agri-api/compare/v1.18.0...v1.19.0) (2026-05-28)

### Features

* **api:** scaffold django-ninja /api/v2 surface
  ([#94](https://github.com/AgriLogy/agri-api/issues/94))
  ([2af3e3f](https://github.com/AgriLogy/agri-api/commit/2af3e3fa512f48deb5ad6bd15beee007204122c4))

### Features

- **api**: Scaffold django-ninja /api/v2 surface
  ([#94](https://github.com/AgriLogy/agri-api/pull/94),
  [`2af3e3f`](https://github.com/AgriLogy/agri-api/commit/2af3e3fa512f48deb5ad6bd15beee007204122c4))

Closes #93.

Per memory `agri-api-fastapi-style`: new agri-api endpoints land as FastAPI-style routers via
  [django-ninja](https://django-ninja.dev/). Legacy DRF `APIView` classes under `/api/...` and
  `/auth/...` keep running and migrate incrementally.

## What lands

- `django-ninja>=1.4` (installs 1.6.2); `uv.lock` refreshed. - `back/agriBack/api/` package: -
  `__init__.py` — `NinjaAPI` instance, JWT auth applied by default, auto OpenAPI at `/api/v2/docs`.
  - `auth.py` — `JwtAuth(HttpBearer)` wrapping simplejwt's `AccessToken`; attaches the resolved user
  to `request.user`. - `routers/sensors.py` — first router: `GET /api/v2/sensors/keys` returns the
  35-entry `SENSOR_KEY_REGISTRY` as typed pydantic envelopes (`SensorKey`, `SensorKeyList`). -
  `tests/test_sensor_keys.py` — Django `TestCase` covering 401-unauth + 200-authed paths. - Mount
  `v2_api.urls` in `agriBack/urls.py` ahead of the legacy `/api` include.

## Verification

- `manage.py check` clean. - Smoke against running stack: `GET /api/v2/sensors/keys` → 401 unauth,
  200 with simplejwt access token; 35 items, `{key, label, unit, type}` shape. - `manage.py test
  agriBack.api.tests` — 2/2 pass. - `/api/v2/docs` serves the auto OpenAPI UI.

## Convention going forward

- View bodies stay 3 lines: parse → call one agri-core handler → return. - Pydantic v2 schemas for
  request/response. - Per-app routers live in `back/agriBack/api/routers/<domain>.py` for now; we'll
  move them under each `apps/<x>/` directory as more land.


## v1.18.0 (2026-05-28)

### Chores

- **release**: 1.18.0 [skip ci]
  ([`df0a7cd`](https://github.com/AgriLogy/agri-api/commit/df0a7cd50923ce71b0d3fba25512143e9943e8cd))

# [1.18.0](https://github.com/AgriLogy/agri-api/compare/v1.17.0...v1.18.0) (2026-05-28)

### Features

* **notifications:** lift the email composer into agri-core
  ([#92](https://github.com/AgriLogy/agri-api/issues/92))
  ([9cbae44](https://github.com/AgriLogy/agri-api/commit/9cbae44e8d9d3f4fbf674b9ac3851398b2a25cce))

### Features

- **notifications**: Lift the email composer into agri-core
  ([#92](https://github.com/AgriLogy/agri-api/pull/92),
  [`9cbae44`](https://github.com/AgriLogy/agri-api/commit/9cbae44e8d9d3f4fbf674b9ac3851398b2a25cce))

Closes #91.

Fourth handler lift. Pairs with [AgriLogy/agri-core#8](https://github.com/AgriLogy/agri-core/pull/8)
  (0.4.0).

## Changes

- Drop `_fmt` + most of `_format_message`. Import `compose_notification_email` from
  `agri.core.notifications` instead. - Rewrite `perform_calculations(user)` as a 3-liner. - Keep
  `should_notify(user)` (Django cadence check) and a 4-line `_format_message` shim for
  backward-compat with `analytics/tests/test_notification_helper.py`. - `uv.lock` → agri-core 0.4.0
  (`5be37dd`). - File shrinks **55 lines (89 → 34)**.

## Verification

- `manage.py check` clean. - `manage.py test analytics.tests.test_notification_helper apps.users
  analytics` — 131/131 pass.

## Cumulative Phase 6

Four handlers in agri-core: `field_snapshot`, `compute_zone_et0`, alerts evaluator,
  `compose_notification_email`. Cumulative agri-api file delta: agronomy.py + alerts.py +
  notification_helper.py: **1,585 → 596 lines** (-989, -62%).


## v1.17.0 (2026-05-28)

### Chores

- **release**: 1.17.0 [skip ci]
  ([`62c6546`](https://github.com/AgriLogy/agri-api/commit/62c65465e88fc9f8dcd071b0004877949f494af9))

# [1.17.0](https://github.com/AgriLogy/agri-api/compare/v1.16.0...v1.17.0) (2026-05-28)

### Features

* **alerts:** lift the alert evaluator into agri-core
  ([#90](https://github.com/AgriLogy/agri-api/issues/90))
  ([cd9eddb](https://github.com/AgriLogy/agri-api/commit/cd9eddbdd85ef66a13ea23551f66351bf3eadb3f))

### Features

- **alerts**: Lift the alert evaluator into agri-core
  ([#90](https://github.com/AgriLogy/agri-api/pull/90),
  [`cd9eddb`](https://github.com/AgriLogy/agri-api/commit/cd9eddbdd85ef66a13ea23551f66351bf3eadb3f))

Closes #89.

Third handler lift per memory `project_agri_core_architecture`. Pairs with
  [AgriLogy/agri-core#6](https://github.com/AgriLogy/agri-core/pull/6) (0.3.0).

## Changes

- Drop the `SENSOR_KEY_REGISTRY` (225 lines), condition constants, `evaluate`, `LatestReading`, and
  the original `suggest_alert` payload assembly. - Re-export the moved symbols from
  `agri.core.alerts` so existing callers (views, tasks, serializers, tests) keep importing from
  `analytics.alerts` unchanged. - Rewrite `evaluate_alert(alert, value)` as a 1-line wrapper that
  packs `AlertSpec(condition, threshold)` from the Django row. - Rewrite `suggest_alert(user, ...)`
  as a Django adapter — fetch recent readings via the ORM, call `suggested_alert_payload`. - Keep
  Django-coupled pieces intact: `get_sensor_model`, `latest_value_for`, `recent_triggers_for_user`,
  `dispatch_alerts_for_reading` (incl. the conditional-UPDATE grace gate + Celery enqueue),
  `grace_period_seconds_for`. - Refresh `uv.lock` to agri-core 0.3.0 (`61c54ce`). - File shrinks
  **269 lines (553 → 284)**.

## Verification

- `manage.py check` clean. - `manage.py test analytics.tests.test_alerts apps.users analytics` —
  131/131 pass. - Stack still up: bridge `/health` 200, django `/admin/login/` 200.

## Cumulative Phase 6 progress

Three handlers now in agri-core: `field_snapshot`, `compute_zone_et0`, and the alerts evaluator.
  ~1,200 lines of business logic moved out of agri-api while every external import path stays valid
  via re-exports.


## v1.16.0 (2026-05-28)

### Chores

- **release**: 1.16.0 [skip ci]
  ([`45f105f`](https://github.com/AgriLogy/agri-api/commit/45f105f95337ed6ba9a0182899cba1c10f62eb19))

# [1.16.0](https://github.com/AgriLogy/agri-api/compare/v1.15.1...v1.16.0) (2026-05-28)

### Features

* **agronomy:** lift compute_et0_for_zone into agri-core
  ([#88](https://github.com/AgriLogy/agri-api/issues/88))
  ([df461eb](https://github.com/AgriLogy/agri-api/commit/df461eb6a041673019ca0ee1080307943f3d09a9))

### Features

- **agronomy**: Lift compute_et0_for_zone into agri-core
  ([#88](https://github.com/AgriLogy/agri-api/pull/88),
  [`df461eb`](https://github.com/AgriLogy/agri-api/commit/df461eb6a041673019ca0ee1080307943f3d09a9))

Closes #87.

Second handler lift per memory `project_agri_core_architecture`. Pairs with
  [AgriLogy/agri-core#4](https://github.com/AgriLogy/agri-core/pull/4) (0.2.0).

## What changes

- Drop the FAO-56 constants + 17 pure-math helpers from `back/agriBack/agronomy.py`. - Drop the
  duplicate `ZoneEt0` dataclass. - Re-export every moved symbol + the new `Et0Inputs` /
  `compute_zone_et0` from `agri.core.agronomy` so existing callers (incl.
  `analytics.tests.test_agronomy` unit tests) keep working. - Rewrite `compute_et0_for_zone(zone,
  end=None)` as a 25-line Django adapter (fetch 5 sensor averages via `_avg`, pack `Et0Inputs`, call
  the handler). - Refresh `uv.lock` to agri-core 0.2.0 (`867cb00`). - File shrinks 416 lines
  (cumulative 943 → 278 over the Phase 6 lifts).

## Verification

- `manage.py check` clean. - `manage.py test analytics.tests.test_agronomy` — 48/48 pass. -
  `manage.py test apps.users analytics` — 131/131 pass. - Stack smoke: django `/admin/login/` 200,
  bridge `/health` 200, all containers healthy.


## v1.15.1 (2026-05-28)

### Chores

- **bridge**: Point compose at sibling agri-bridge repo
  ([#86](https://github.com/AgriLogy/agri-api/pull/86),
  [`3e4e794`](https://github.com/AgriLogy/agri-api/commit/3e4e7941681f2c33cc5b0a0950bd7ee76149d0ac))

Closes #85.

The 9090 ingest gateway is now its own repo at
  [AgriLogy/agri-bridge](https://github.com/AgriLogy/agri-bridge). This PR is the agri-api side:

- Delete `Devops/server/` (Node sources, tests, Dockerfile, package-lock). - `docker-compose.yml` —
  change the agri-bridge build context from `./Devops/server` → `../agri-bridge` (sibling checkout,
  matches the pattern we already use for agri-api ↔ agri-core dev). - `docs/flows/data-ingestion.md`
  — fix two stale `Devops/server/server.js` references.

## Verification

- `docker compose build --no-cache agri-bridge` succeeds. - All containers up + healthy:
  agri-bridge, agri-api-web (healthy), worker, beat, redis, mailpit. - Bridge `GET /health` returns
  200. - Django `/admin/login/` returns 200. - agri-bridge vitest: 13/13. - agri-api `manage.py test
  apps.users analytics apps.bivocom`: 131 passing tests (11 pre-existing pytest-import errors
  unrelated, predate this PR).

- **release**: 1.15.1 [skip ci]
  ([`f5f3b02`](https://github.com/AgriLogy/agri-api/commit/f5f3b021f1c74791f603b62e458c4d9d2fa97bdc))

## [1.15.1](https://github.com/AgriLogy/agri-api/compare/v1.15.0...v1.15.1) (2026-05-28)


## v1.15.0 (2026-05-28)

### Chores

- **release**: 1.15.0 [skip ci]
  ([`bff8142`](https://github.com/AgriLogy/agri-api/commit/bff814266105329d2c2f5650c4acf6f57d231bb2))

# [1.15.0](https://github.com/AgriLogy/agri-api/compare/v1.14.1...v1.15.0) (2026-05-28)

### Features

* **agronomy:** lift field_snapshot into agri-core
  ([#84](https://github.com/AgriLogy/agri-api/issues/84))
  ([c1b8444](https://github.com/AgriLogy/agri-api/commit/c1b8444b038e1d38edbf16c31b1b70ab1b533438))

### Features

- **agronomy**: Lift field_snapshot into agri-core
  ([#84](https://github.com/AgriLogy/agri-api/pull/84),
  [`c1b8444`](https://github.com/AgriLogy/agri-api/commit/c1b8444b038e1d38edbf16c31b1b70ab1b533438))

Closes #83.

First handler lift per memory `project_agri_core_architecture`. Pairs with
  [AgriLogy/agri-core#2](https://github.com/AgriLogy/agri-core/pull/2) which shipped agri-core 0.1.0
  with the framework-agnostic handler.

## What changes here

- Re-export `DEFAULT_KC`, `DEFAULT_CRITICAL_SOIL_MOISTURE_PCT`, the default efficiency constants,
  the water-balance math (`effective_rainfall_mm`, `etc_mm`, `update_daily_depletion`,
  `cumulative_dr_after_missed_days`), the new DTOs (`ZoneParams`, `SensorAggregates`,
  `FieldInputs`), the `IrrigationDecision` struct and `irrigation_decision_dr` from
  `agri.core.agronomy`. Existing callers (incl. `test_agronomy.py`'s unit tests) keep working
  unchanged. - Rewrite `field_snapshot(user, ...)` as a thin Django adapter — fetch zone + sensors
  via the ORM, pack into `FieldInputs`, call `agri.core.agronomy.field_snapshot`, return its dict. -
  Refresh `uv.lock` to agri-core 0.1.0 (`88e761f`). - File shrinks 541 lines (943 → 487).

## What stays here

The FAO-56 hourly math (`penman_monteith_hourly_mm`, helpers) and `compute_et0_for_zone` — separate
  consumer; lifted later when its caller (the Celery hourly task) also moves.

## Verification

- `manage.py check` clean. - `manage.py test analytics.tests.test_agronomy` — 48/48 pass. -
  `manage.py test apps.users analytics` — 131/131 pass. - Email key contract preserved (locked in by
  an explicit agri-core test).


## v1.14.1 (2026-05-28)

### Chores

- Pin agri-core into agri-api as a runtime dep ([#82](https://github.com/AgriLogy/agri-api/pull/82),
  [`a5830e8`](https://github.com/AgriLogy/agri-api/commit/a5830e822a171b9e13775b74f1cf9f4d81a3bb8b))

Closes #81.

Bring `agri-core` into the dependency graph so subsequent PRs can move handler logic out of agri-api
  views and into framework-agnostic functions in `agri-core`, per memory
  `project_agri_core_architecture`.

## Changes

- `back/pyproject.toml`: add `agri-core @ git+https://github.com/AgriLogy/agri-core.git@main`. -
  `back/Dockerfile`: install `git` so `uv sync` can resolve VCS deps. - `back/uv.lock`: regenerated
  — pins to agri-core 0.0.1 at SHA `e5b84dc`.

## Verification

- `docker compose build agri-api-web` succeeds. - `docker exec agri-api-web python -c "import
  agri.core; print(agri.core.__version__)"` prints `0.0.1`. - `manage.py check` clean.

`AgriLogy/agri-core` is now public so no token wrangling is needed in CI / prod.

Out of scope: lifting any actual handler into agri-core — that happens in follow-up PRs.

- **release**: 1.14.1 [skip ci]
  ([`fae7b08`](https://github.com/AgriLogy/agri-api/commit/fae7b0840c2f19bea295f047f0e585c51cdc0f73))

## [1.14.1](https://github.com/AgriLogy/agri-api/compare/v1.14.0...v1.14.1) (2026-05-28)


## v1.14.0 (2026-05-28)

### Chores

- **release**: 1.14.0 [skip ci]
  ([`db8be37`](https://github.com/AgriLogy/agri-api/commit/db8be37b5dd74fd91bfc82801b3012577aefca11))

# [1.14.0](https://github.com/AgriLogy/agri-api/compare/v1.13.0...v1.14.0) (2026-05-28)

### Features

* **sensors:** extract sensor models into apps/sensors/
  ([#80](https://github.com/AgriLogy/agri-api/issues/80))
  ([06b9266](https://github.com/AgriLogy/agri-api/commit/06b92661d80ab65ca2ea97238adfbce5c955dd97))

### Features

- **sensors**: Extract sensor models into apps/sensors/
  ([#80](https://github.com/AgriLogy/agri-api/pull/80),
  [`06b9266`](https://github.com/AgriLogy/agri-api/commit/06b92661d80ab65ca2ea97238adfbce5c955dd97))

Closes #79.

Phase 5d, part 1 of 2. Lift the 38 sensor model classes out of `analytics/models.py` into a new
  `apps/sensors/` package, behind a re-export bridge so no caller has to change yet.

## How the move stays invisible to the DB

- Every concrete sensor inherits from an abstract `_SensorBase`. Its `Meta.app_label = "analytics"`
  keeps Django thinking the models belong to the analytics app — so the migration history
  (`analytics/migrations/0001` → `0058`), the `analytics_*` table names, and FK strings like
  `"analytics.Zone"` all keep working. - FKs to `Zone` use the string form `"analytics.Zone"`
  instead of the class import, to avoid a circular import with the re-export. -
  `UserSensorUnitPreference` had its own `Meta(unique_together, indexes)` and lost the inherited
  `app_label` — fixed by explicitly setting `app_label = "analytics"` in its `Meta`.

## What moves

- 34 registry sensors (Et0Calculated, Et0Weather, PrecipitationRate, HumidityWeather, WindSpeed,
  SolarRadiation, PressureWeather, WindDirection, TemperatureWeather, ECSoil{High,Medium,Low},
  SoilMoisture{High,Medium,Low}, PhSoil, SoilTemperature{High,Medium,Low}, WaterFlowSensor,
  WaterPressureSensor, WaterECSensor, PhWaterSensor, ElectricityConsumptionSensor,
  LeafMoistureSensor, LeafTemperatureSensor, MultiDepthSoilMoistureSensor, LargeFruitDiameterSensor,
  WaterLevelSensor, SoilSalinitySensor, SoilConductivitySensor, NpkSensor, FruitSizeSensor,
  EcSalinitySensor). - 4 adjacent (SensorColor, SensorLocation, VPDWeather,
  UserSensorUnitPreference). - The `@receiver(post_save, sender=User)` handler that auto-creates
  `GraphName` + `SensorColor` for new users moves back to `analytics/models.py` since `GraphName`
  lives there.

## Verification

- `manage.py check` — clean. - `migrate --check` — no pending. - `makemigrations --dry-run` — `No
  changes detected`. - 141 tests run (10 errors are the same pre-existing `import pytest` import in
  `analytics/tests/test_manager_affirmation.py`; unrelated). - `curl /admin/login/` — 200. - `curl
  /api/sensors/precipitationrate/` — 401 (auth required, identical to pre-rename).

## Part 2 (follow-up)

- Migrate all callers from `from analytics.models import <Sensor>` to `from apps.sensors.models
  import <Sensor>`. - Lift `sensor_registry`, sensor views, sensor serializers, sensor admin into
  `apps/sensors/`. - Drop the re-export block from `analytics/models.py`.


## v1.13.0 (2026-05-28)

### Chores

- **release**: 1.13.0 [skip ci]
  ([`ce50ec5`](https://github.com/AgriLogy/agri-api/commit/ce50ec51323dd6053478b1d318f82fb0d1d75b21))

# [1.13.0](https://github.com/AgriLogy/agri-api/compare/v1.12.7...v1.13.0) (2026-05-28)

### Features

* **users:** rename CustomUser package to apps/users
  ([#78](https://github.com/AgriLogy/agri-api/issues/78))
  ([8b75c6e](https://github.com/AgriLogy/agri-api/commit/8b75c6e7856519408e5392650f91a1eec534937c))

### Features

- **users**: Rename CustomUser package to apps/users
  ([#78](https://github.com/AgriLogy/agri-api/pull/78),
  [`8b75c6e`](https://github.com/AgriLogy/agri-api/commit/8b75c6e7856519408e5392650f91a1eec534937c))

Closes #77.

Phase 5c of the senior-dev refactor. Move the user-management Django app under `apps/` so the
  structure matches `apps.bivocom` and `apps.lorawan.chirpstack`.

## Approach

Pure package rename. **Django app_label stays `CustomUser`** via an explicit `UsersConfig.label =
  "CustomUser"`. That keeps:

- `AUTH_USER_MODEL = "CustomUser.CustomUser"` resolving without a migration - All FK strings like
  `"CustomUser.<Model>"` resolving - `django_migrations` rows for the existing 6 migrations valid —
  no fake re-applies

Only the Python import path changes (`CustomUser.*` → `apps.users.*`).

## Changes

- Git rename `back/CustomUser/` → `back/apps/users/` (history preserved). - `apps.py` — class
  renamed `CustomuserConfig` → `UsersConfig`; `name = "apps.users"`; explicit `label =
  "CustomUser"`. - `INSTALLED_APPS` — `"CustomUser"` → `"apps.users.apps.UsersConfig"`. - URL
  includes — `"CustomUser.urls"` / `"CustomUser.admin_urls"` → `apps.users.*` equivalents. -
  Cross-app imports — `agriBack/tasks.py`, `scripts/et0_every_hour.py`,
  `analytics/tests/test_notification_helper.py` (one was a `mock.patch` target string).

## Verification

- `manage.py check` clean. - `manage.py migrate --check` reports no pending. - `manage.py
  showmigrations CustomUser` shows all 6 migrations still applied. - `DJANGO_ENV=test manage.py test
  apps.users analytics` — 131 pass, 0 failures. The 10 errors all come from one pre-existing test
  file that imports `pytest` at module level (`analytics/tests/test_manager_affirmation.py`) —
  `pytest` isn't in the image. Unrelated to this rename; will be addressed by adding pytest to the
  dev deps in a separate PR.


## v1.12.7 (2026-05-28)

### Chores

- **analytics**: Drop unused AdminHeaderAPIView
  ([#76](https://github.com/AgriLogy/agri-api/pull/76),
  [`bc01b86`](https://github.com/AgriLogy/agri-api/commit/bc01b86ecdfec7c312e8effef61970dadb00aba5))

Closes #75.

`AdminHeaderAPIView` was exported by `analytics/adminviews.py` but never wired into a URL —
  `/api/header/` routes to `analytics.views.HeaderAPIView`, a different class.

Drops the class + the imports that were only there to support it (`Q`, `AllowAny`,
  `IsAuthenticated`, the `User = get_user_model()` alias). The legacy `ActiveGraphAdminAPIView`
  stays — it's still wired in `urls.py` for the in-flight front-end migration.

Closes out Phase 1 of the senior-dev playbook. The other Phase 1 items (cruft removal, seed scripts
  relocation, `.gitignore`/`.dockerignore`) were already done by prior sessions.

Verified `manage.py check` clean and `ruff check` clean.

- **release**: 1.12.7 [skip ci]
  ([`5e6acd3`](https://github.com/AgriLogy/agri-api/commit/5e6acd39d5d439785bd1ff00c4271cfad1133449))

## [1.12.7](https://github.com/AgriLogy/agri-api/compare/v1.12.6...v1.12.7) (2026-05-28)


## v1.12.6 (2026-05-28)

### Chores

- **docs**: Refer to renamed sibling agri-db ([#74](https://github.com/AgriLogy/agri-api/pull/74),
  [`212b41e`](https://github.com/AgriLogy/agri-api/commit/212b41e0a049c7291be70ddff973d46b1810c826))

Closes #73.

`agrilogy-db` was renamed to `agri-db` on GitHub, and the local working tree dir was renamed to
  match. The 7 stale references here were `cd ../agrilogy-db` and friends — now they point to the
  right path.

CHANGELOG.md left untouched (historical; redirects handle it).

- **release**: 1.12.6 [skip ci]
  ([`a539d85`](https://github.com/AgriLogy/agri-api/commit/a539d853cbcb1143e89127a3061e69a4f358bfb0))

## [1.12.6](https://github.com/AgriLogy/agri-api/compare/v1.12.5...v1.12.6) (2026-05-28)


## v1.12.5 (2026-05-28)

### Chores

- **release**: 1.12.5 [skip ci]
  ([`677f746`](https://github.com/AgriLogy/agri-api/commit/677f7467e576137b5ba3c21c1e4d47b3627cc559))

## [1.12.5](https://github.com/AgriLogy/agri-api/compare/v1.12.4...v1.12.5) (2026-05-28)

- **scripts**: Point project-sync examples at agri-api
  ([#72](https://github.com/AgriLogy/agri-api/pull/72),
  [`d432d3e`](https://github.com/AgriLogy/agri-api/commit/d432d3efc303419d7d591a2f6782649d6e01df4f))

Closes #71.

Post-rename cleanup: the three example URLs in `scripts/project-sync.sh`'s header still printed
  `agrilogy-back`. GitHub redirects the old name, but the cleaner copy-paste target is `agri-api`.

Historical CHANGELOG entries are left as-is — rewriting release notes is wrong, and the redirects
  handle the links.


## v1.12.4 (2026-05-28)

### Bug Fixes

- **entrypoint**: Match USE_POSTGRES case-insensitively
  ([#70](https://github.com/AgriLogy/agri-api/pull/70),
  [`243d3fd`](https://github.com/AgriLogy/agri-api/commit/243d3fd8a11294aea15fb437a89d19fd17af5c88))

Closes #69.

`back/docker-entrypoint.sh` only treated `USE_POSTGRES=True` as truthy, so prod
  (`USE_POSTGRES=true`) was silently skipping `wait_for_postgres`. Worked at last deploy because
  `agrydata` was already up, but could race on a cold start.

Switched to a case statement that accepts any casing of `true` and defaults to skipping the wait.

Verified manually:

``` True -> WAIT true -> WAIT TRUE -> WAIT tRuE -> WAIT false -> SKIP False -> SKIP (unset) -> SKIP
  ```

`bash -n` clean.

### Chores

- **release**: 1.12.4 [skip ci]
  ([`1549dd0`](https://github.com/AgriLogy/agri-api/commit/1549dd0207d56387503ca789b34a8e87d0db657b))

## [1.12.4](https://github.com/AgriLogy/agrilogy-back/compare/v1.12.3...v1.12.4) (2026-05-28)

### Bug Fixes

* **entrypoint:** match USE_POSTGRES case-insensitively
  ([#70](https://github.com/AgriLogy/agrilogy-back/issues/70))
  ([243d3fd](https://github.com/AgriLogy/agrilogy-back/commit/243d3fd8a11294aea15fb437a89d19fd17af5c88))


## v1.12.3 (2026-05-28)

### Chores

- **ci**: Enable uv cache and enforce --frozen sync
  ([#12](https://github.com/AgriLogy/agri-api/pull/12),
  [`355ed39`](https://github.com/AgriLogy/agri-api/commit/355ed39ef73e8d4504c2173ec5dc2242aa0d938a))

Closes #68.

`back/uv.lock` is now committed on `main`, so this finally lands the CI cache flip that was deferred
  at the time the PR was first opened:

- Re-enable `enable-cache: true` + `cache-dependency-glob: back/uv.lock` in setup-uv. - Drop the `uv
  sync --frozen || uv sync` fallback in CI; require `--frozen`. - Tighten `make install` to `uv sync
  --frozen` for parity with CI.

Rebased onto current main; the original 940-line lockfile diff is gone because that work landed via
  another path.

- **release**: 1.12.3 [skip ci]
  ([`9eb53f9`](https://github.com/AgriLogy/agri-api/commit/9eb53f9ad185ccd6aeb005fab73a8286b00cdd47))

## [1.12.3](https://github.com/AgriLogy/agrilogy-back/compare/v1.12.2...v1.12.3) (2026-05-28)


## v1.12.2 (2026-05-28)

### Bug Fixes

- **compose**: Stop overriding EMAIL_HOST/EMAIL_PORT from env_file
  ([#47](https://github.com/AgriLogy/agri-api/pull/47),
  [`f30dc84`](https://github.com/AgriLogy/agri-api/commit/f30dc84af14602ade1da219162598c73d2d9ce67))

Closes #46

## Summary Drop the `EMAIL_HOST`/`EMAIL_PORT` overrides from `docker-compose.yml` on all three
  backend services (agrybackend, celery-worker, celery-beat). The `${EMAIL_HOST:-mailpit}` pattern
  resolves against the shell env at compose-up time — which is empty in CI/prod — so it pinned
  `EMAIL_HOST=mailpit` inside containers and shadowed whatever `back/.env` said. `env_file:
  ./back/.env` is now the single source of truth.

## Why Caught while wiring up real Gmail SMTP delivery in prod: adding `EMAIL_HOST=smtp.gmail.com`
  to `back/.env` had no effect because the compose override silently won. Confirmed by checking
  `docker exec agrybackend env | grep EMAIL_HOST` after the change — value now matches `back/.env`.

## Test plan - [ ] On staging after merge: `docker exec agrybackend env | grep EMAIL_HOST` returns
  the `back/.env` value (or empty if unset), never `mailpit` unless `back/.env` actually says so. -
  [ ] Local dev users wanting mailpit add `EMAIL_HOST=mailpit` / `EMAIL_PORT=1025` to their local
  `back/.env` once. - [ ] Promote main→alpha is unnecessary here (back deploys from main directly).

### Chores

- **release**: 1.12.2 [skip ci]
  ([`c9c103f`](https://github.com/AgriLogy/agri-api/commit/c9c103f9d6250628d9598d71978ddadeaa2f472a))

## [1.12.2](https://github.com/AgriLogy/agrilogy-back/compare/v1.12.1...v1.12.2) (2026-05-28)

### Bug Fixes

* **compose:** stop overriding EMAIL_HOST/EMAIL_PORT from env_file
  ([#47](https://github.com/AgriLogy/agrilogy-back/issues/47))
  ([f30dc84](https://github.com/AgriLogy/agrilogy-back/commit/f30dc84af14602ade1da219162598c73d2d9ce67)),
  closes [#46](https://github.com/AgriLogy/agrilogy-back/issues/46)


## v1.12.1 (2026-05-28)

### Chores

- **project**: Add project-sync.sh helper + env-example fix
  ([#67](https://github.com/AgriLogy/agri-api/pull/67),
  [`6d18707`](https://github.com/AgriLogy/agri-api/commit/6d18707e9ff183d4d4cac969c158428cec265611))

Part of #48. Quick follow-up to PR #66.

## What's new - `scripts/project-sync.sh` — wraps the ProjectV2 GraphQL API for adding issues/PRs to
  AgriLogy/projects/1 with Fibonacci story-point estimates and Status updates - `back/env-example` —
  documents that `ALLOWED_HOSTS` must include `agri-api-web` for the bridge to forward (caught when
  wiring up the bridge in #66)

## Usage ```bash ./scripts/project-sync.sh add <issue-or-pr-url> --estimate 5 --status "In review"
  ./scripts/project-sync.sh close <url> # sets Status=Done ```

- **release**: 1.12.1 [skip ci]
  ([`50781f8`](https://github.com/AgriLogy/agri-api/commit/50781f84d0db3bacc8fb1a3f6dd8a69116ac546d))

## [1.12.1](https://github.com/AgriLogy/agrilogy-back/compare/v1.12.0...v1.12.1) (2026-05-28)


## v1.12.0 (2026-05-28)

### Chores

- **release**: 1.12.0 [skip ci]
  ([`8dd24a9`](https://github.com/AgriLogy/agri-api/commit/8dd24a90405d4759c16f279f666776413ac00ea9))

# [1.12.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.11.0...v1.12.0) (2026-05-28)

### Features

* **bridge:** professionalize the 9090 ingest gateway
  ([#66](https://github.com/AgriLogy/agrilogy-back/issues/66))
  ([6673403](https://github.com/AgriLogy/agrilogy-back/commit/6673403f6b17bc2fde158f0dccfb9773193422ee))

### Features

- **bridge**: Professionalize the 9090 ingest gateway
  ([#66](https://github.com/AgriLogy/agri-api/pull/66),
  [`6673403`](https://github.com/AgriLogy/agri-api/commit/6673403f6b17bc2fde158f0dccfb9773193422ee))

Closes #65. Part of #48.

Replaces the provisional file-based bridge with a proper Express + zod + pino HTTP gateway. The
  bridge now translates Router0X-format payloads from field devices into the pydantic-typed Bivocom
  v1 format and forwards to `/api/v1/bivocom/uplink`.

## Before - Flat-file storage at `shared_data/requests.json` — race conditions on concurrent writes,
  unbounded growth, parse-failure wiped history - Returns 200 even when the backend forward fails
  (silent data loss) - Hand-rolled http module, no input validation, no tests, no `package.json` -
  Targets the legacy `/api/sensors/weather/ingest/` endpoint

## After - **Stateless** — no file persistence; Postgres (via agri-api) is the source of truth -
  **Validates** Router0X input with zod (any new sensor key passes through; non-finite values
  rejected) - **Translates** to Bivocom v1: `device_id = `router-user-${user}``, `tags =
  {sensor_key: value}` - **Forwards** with timeout + exponential-backoff retry; permanent 4xx does
  NOT retry - **Returns** 202/400/502 with proper bodies (no more silent failures) - **Logs**
  structured JSON via pino - **Tested**: 13 vitest tests (schema + transform unit + server
  integration) - **Multi-stage** Dockerfile, runs as non-root, no build tooling in runtime image -
  Removes `server.py`, `requests.json`, `curl*.txt` — legacy artifacts gone

## Verified - `npm test` → 13/13 PASSED - Full smoke (`scripts/smoke.sh`) → **11/11 green**
  including the new bridge e2e check (Router0X → bridge → /api/v1/bivocom/uplink → 202)

## Out of scope - Shared-secret auth on the bridge (`X-Agri-Token`) — follow-up; will be enforced on
  both bridge and the bivocom endpoint together - Persistence of failed forwards (queue +
  retry-later) — not needed at current volume; in-process retry suffices


## v1.11.0 (2026-05-28)

### Chores

- **release**: 1.11.0 [skip ci]
  ([`6a04a00`](https://github.com/AgriLogy/agri-api/commit/6a04a009d399d1ae1abbdbf5d5189edd64ab976d))

# [1.11.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.10.0...v1.11.0) (2026-05-28)

### Features

* **lorawan/chirpstack:** pydantic-typed webhook for ChirpStack v4 uplink
  ([#64](https://github.com/AgriLogy/agrilogy-back/issues/64))
  ([f64f514](https://github.com/AgriLogy/agrilogy-back/commit/f64f514549e8264302409859d4bd3513d19feb28))

### Features

- **lorawan/chirpstack**: Pydantic-typed webhook for ChirpStack v4 uplink
  ([#64](https://github.com/AgriLogy/agri-api/pull/64),
  [`f64f514`](https://github.com/AgriLogy/agri-api/commit/f64f514549e8264302409859d4bd3513d19feb28))

Closes #63. Part of #48.

LoRaWAN ingest placeholder ready for the gateway arriving ≤ 2 days. Real normalization wires in at
  Phase 6.5 via `agri.core.devices.lorawan.chirpstack.ChirpStackAdapter`.

## What's new `back/apps/lorawan/chirpstack/`: - `schemas.py` — `ChirpStackUplink` with nested
  `ChirpStackDeviceInfo` + `ChirpStackRxInfo`. `extra='ignore'` so future ChirpStack fields don't
  break us. DevEUI validated as exactly 16 hex chars. - `views.py` — DRF view: 202 on valid,
  400+details on rejection - `urls.py` — `POST /api/v1/lorawan/chirpstack/uplink` - `tests.py` — 6
  tests (4 schema + 2 integration)

## Verified - `pytest apps/lorawan/chirpstack/tests.py` → 6/6 PASSED - `scripts/smoke.sh` → 8/8
  green

## Configure in ChirpStack ``` Endpoint URL:
  https://api.agrologyy.com/api/v1/lorawan/chirpstack/uplink Headers: X-Agri-Token: <shared-secret>
  (TODO: enforce in follow-up) ```

## Out of scope - Real normalization → Phase 6.5 - TTN / Helium → slot in alongside chirpstack/
  later


## v1.10.0 (2026-05-28)

### Chores

- **release**: 1.10.0 [skip ci]
  ([`a1a12dc`](https://github.com/AgriLogy/agri-api/commit/a1a12dcc5772c71a78ab4b860117761e0810dd7e))

# [1.10.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.9.0...v1.10.0) (2026-05-28)

### Features

* **bivocom:** apps/bivocom/ Django app with pydantic-typed ingest
  ([#62](https://github.com/AgriLogy/agrilogy-back/issues/62))
  ([e3efb2a](https://github.com/AgriLogy/agrilogy-back/commit/e3efb2a90fa193f88afc1c9c9d30f67ecd719fa0))

### Features

- **bivocom**: Apps/bivocom/ Django app with pydantic-typed ingest
  ([#62](https://github.com/AgriLogy/agri-api/pull/62),
  [`e3efb2a`](https://github.com/AgriLogy/agri-api/commit/e3efb2a90fa193f88afc1c9c9d30f67ecd719fa0))

Closes #61. Part of #48.

First per-hardware-family ingest app. Structural foundation — real normalization wires in at Phase
  6.5 via `agri.core.devices.BivocomAdapter`.

## What's new `back/apps/bivocom/` - `schemas.py` — pydantic `BivocomUplink` (extra='forbid',
  tag-count ≥ 1) and `BivocomUplinkResponse` - `views.py` — DRF view: 202 on valid, 400+details on
  pydantic rejection - `urls.py` — `POST /api/v1/bivocom/uplink` - `tests.py` — 5 pytest tests (3
  schema, 2 integration)

`back/agriBack/` - settings/base.py: INSTALLED_APPS += apps.bivocom - urls.py: include the new route

`scripts/smoke.sh`: 2 new checks (valid 202, invalid 400)

## Per-user constraint All new Python typed with pydantic. `BivocomUplink` and
  `BivocomUplinkResponse` are `BaseModel` subclasses; field-level validation (min/max length, ge=0,
  etc.) lives in the schema, not the view.

## Verified - `pytest apps/bivocom/tests.py` → 5/5 PASSED - `scripts/smoke.sh` → 6/6 green

## Out of scope - Real adapter / normalization → Phase 6.5 - Persisting SensorReading rows → Phase 6
  - Shared-secret auth on the webhook → follow-up


## v1.9.0 (2026-05-28)

### Chores

- **release**: 1.9.0 [skip ci]
  ([`95365cb`](https://github.com/AgriLogy/agri-api/commit/95365cb8cf7be73dbe87c3e79377b2e92ffe0a8a))

# [1.9.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.8.1...v1.9.0) (2026-05-28)

### Features

* **errors:** AgriError hierarchy + DRF exception handler
  ([#60](https://github.com/AgriLogy/agrilogy-back/issues/60))
  ([8eec582](https://github.com/AgriLogy/agrilogy-back/commit/8eec582b0d9d736280f75bceaba115ee3edab5b5))

### Features

- **errors**: Agrierror hierarchy + DRF exception handler
  ([#60](https://github.com/AgriLogy/agri-api/pull/60),
  [`8eec582`](https://github.com/AgriLogy/agri-api/commit/8eec582b0d9d736280f75bceaba115ee3edab5b5))

Closes #59. Part of #48.

## What's new - `agriBack/errors.py` — `AgriError` base + 5 subclasses (`AgriNotFoundError` 404,
  `AgriValidationError` 400, `AgriForbiddenError` 403, `AgriConflictError` 409,
  `AgriUnavailableError` 503). Each carries `http_status` + `code` class attrs. -
  `agriBack/exception_handler.py` — maps any `AgriError` → `{"error": {"code", "message"}}` JSON at
  the right status; non-AgriError falls through to DRF default. - `agriBack/settings/base.py` —
  registers `REST_FRAMEWORK[EXCEPTION_HANDLER]`. - `agriBack/tests_errors.py` — **9 pytest tests**
  asserting status + JSON shape per class, empty-message fallback, subclass inheritance.

## Verified - `DJANGO_ENV=test pytest agriBack/tests_errors.py` → 9/9 PASSED - `scripts/smoke.sh` →
  4/4 green

## Out of scope Rewriting existing views to use `AgriError` — incremental during Phase 5/6 (per-app
  extraction).


## v1.8.1 (2026-05-28)

### Bug Fixes

- **phase-2**: Wire DJANGO_ENV through Makefile + entrypoint + compose + env-example
  ([#58](https://github.com/AgriLogy/agri-api/pull/58),
  [`e32b9d4`](https://github.com/AgriLogy/agri-api/commit/e32b9d42a95d49164986acfc5a4e1c3eec3e780a))

Closes #. Part of #48.

PR #56 didn't stage the 4 wiring edits before commit. This brings them in: - `Makefile` test target:
  `DJANGO_ENV=test` - `back/docker-entrypoint.sh`: `export DJANGO_ENV=${DJANGO_ENV:-dev}` -
  `docker-compose.yml`: passes `DJANGO_ENV` to all 3 services - `back/env-example`: documents
  `DJANGO_ENV=dev`

### Chores

- **release**: 1.8.1 [skip ci]
  ([`256e6ea`](https://github.com/AgriLogy/agri-api/commit/256e6ead2d0404503730513554296ea9d2a3a14c))

## [1.8.1](https://github.com/AgriLogy/agrilogy-back/compare/v1.8.0...v1.8.1) (2026-05-28)

### Bug Fixes

* **phase-2:** wire DJANGO_ENV through Makefile + entrypoint + compose + env-example
  ([#58](https://github.com/AgriLogy/agrilogy-back/issues/58))
  ([e32b9d4](https://github.com/AgriLogy/agrilogy-back/commit/e32b9d42a95d49164986acfc5a4e1c3eec3e780a)),
  closes [#56](https://github.com/AgriLogy/agrilogy-back/issues/56)


## v1.8.0 (2026-05-28)

### Chores

- **release**: 1.8.0 [skip ci]
  ([`8042510`](https://github.com/AgriLogy/agri-api/commit/8042510ae00b730798bc7d36b673a4aaffcbd371))

# [1.8.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.7.4...v1.8.0) (2026-05-28)

### Features

* **settings:** split monolithic settings.py into base/dev/prod/test
  ([#56](https://github.com/AgriLogy/agrilogy-back/issues/56))
  ([6a3c764](https://github.com/AgriLogy/agrilogy-back/commit/6a3c764f870683c63cece6f8584b1fbb14136b25))

### Features

- **settings**: Split monolithic settings.py into base/dev/prod/test
  ([#56](https://github.com/AgriLogy/agri-api/pull/56),
  [`6a3c764`](https://github.com/AgriLogy/agri-api/commit/6a3c764f870683c63cece6f8584b1fbb14136b25))

Closes #55. Part of #48.

## Result agriBack/settings.py (334 lines) → agriBack/settings/ package:

| File | Role | |---|---| | `__init__.py` | DJANGO_ENV dispatcher (dev/prod/test) | | `base.py` |
  apps, middleware, REST_FRAMEWORK, SIMPLE_JWT, auth, I18N, static, Celery base + schedule,
  ALERT_GRACE_PERIODS, LOGGING | | `dev.py` | DEBUG=True, Postgres (Supabase), console email | |
  `prod.py` | DEBUG=False, **hard-fails on missing prod secrets**, secure cookies + HSTS + SSL
  redirect | | `test.py` | sqlite `:memory:`, locmem email, eager Celery |

## Other touch-points - `Makefile` test target: `DJANGO_ENV=test` - `docker-entrypoint.sh`: exports
  `DJANGO_ENV=${DJANGO_ENV:-dev}` - `docker-compose.yml`: passes `DJANGO_ENV` to all 3 services -
  `env-example`: documents `DJANGO_ENV`

## Verified (against the running stack) - `DJANGO_ENV=test python manage.py check` → no issues -
  `DJANGO_ENV=dev python manage.py check` → no issues - `DJANGO_ENV=prod` w/o SECRET_KEY →
  `RuntimeError` (loud) - `scripts/smoke.sh` → 4/4 green

## Out of scope - `pydantic-settings` for typed non-Django config → Phase 6/6.5 - Internal
  `agriBack/` → `agriapi/` rename → Phase 9


## v1.7.4 (2026-05-28)

### Bug Fixes

- **phase-1**: Drop Dockerfile run.sh refs + update entrypoint paths
  ([#54](https://github.com/AgriLogy/agri-api/pull/54),
  [`437ca4c`](https://github.com/AgriLogy/agri-api/commit/437ca4c5c741f56dd6a5bf39725df980754f8244))

Closes #53. Part of #48.

PR #52 (Phase 1 cleanup) deleted `back/run.sh` and moved seed scripts to `back/scripts/`, but the
  matching Dockerfile + docker-entrypoint.sh edits were left unstaged at commit time. **`main`
  currently fails to build** with: ``` COPY run.sh /run.sh failed to solve: "/run.sh": not found ```

## Fix - Drop `COPY run.sh` + `chmod /run.sh` from `back/Dockerfile` (incl. the legacy-compat
  comment) - Update `back/docker-entrypoint.sh` to call `python scripts/seed_dev_users.py` and
  `python scripts/seed_dev_data.py` - Add `celerybeat-schedule` to `.gitignore`

## Verified `docker compose build agri-api-web` → success.

### Chores

- **release**: 1.7.4 [skip ci]
  ([`553a04d`](https://github.com/AgriLogy/agri-api/commit/553a04d043aede5ef22137fe03fe4c13108d949e))

## [1.7.4](https://github.com/AgriLogy/agrilogy-back/compare/v1.7.3...v1.7.4) (2026-05-28)

### Bug Fixes

* **phase-1:** drop Dockerfile run.sh refs + update entrypoint paths
  ([#54](https://github.com/AgriLogy/agrilogy-back/issues/54))
  ([437ca4c](https://github.com/AgriLogy/agrilogy-back/commit/437ca4c5c741f56dd6a5bf39725df980754f8244)),
  closes [#52](https://github.com/AgriLogy/agrilogy-back/issues/52)


## v1.7.3 (2026-05-28)

### Chores

- Phase 1 cleanup — remove committed runtime artifacts + dead scripts
  ([#52](https://github.com/AgriLogy/agri-api/pull/52),
  [`0e2a458`](https://github.com/AgriLogy/agri-api/commit/0e2a458f6322b7cf25c292f0f4bcde3d501a704e))

Closes #51. Part of #48.

## What's deleted - `back/db.sqlite3` (committed despite .gitignore) - `back/celerybeat-schedule`
  (runtime state) - `back/requests.json` (stale; runtime lives in shared_data/) - `back/dummy.py`
  (faker seed; only called by run.sh) - `back/run.sh` (legacy entrypoint that ran Django `migrate` —
  schema now in agri-db) - Dockerfile lines for run.sh COPY/chmod

## Moved (git mv, history preserved) - `back/seed_dev_users.py` → `back/scripts/seed_dev_users.py` -
  `back/seed_dev_data.py` → `back/scripts/seed_dev_data.py` - `back/et0_every_hour.py` →
  `back/scripts/et0_every_hour.py` - `docker-entrypoint.sh` updated to call scripts from new path

## Added - `.dockerignore` (image hygiene) - `celerybeat-schedule` in .gitignore

## Out of scope `analytics/adminviews.py` vs `admin_views.py` — verified NOT duplicates (different
  URL files). Separate PR for the rename.

## Verified - `docker compose build agri-api-web` succeeds with the new Dockerfile

- **release**: 1.7.3 [skip ci]
  ([`4bc36db`](https://github.com/AgriLogy/agri-api/commit/4bc36db1ba1576784fcc0a725d79c4d34d86ad92))

## [1.7.3](https://github.com/AgriLogy/agrilogy-back/compare/v1.7.2...v1.7.3) (2026-05-28)


## v1.7.2 (2026-05-28)

### Chores

- Rename agrilogy-back → agri-api (folder + image tag)
  ([#50](https://github.com/AgriLogy/agri-api/pull/50),
  [`4c6002e`](https://github.com/AgriLogy/agri-api/commit/4c6002e81a351ef1371ef3c6a836493474742009))

Closes #49. Part of #48.

## Phase 0 — repo rename for agrilogy-back

First of four repo renames in Phase 0 of the senior-dev refactor plan
  (`~/.claude/skills/senior-dev/refactor/agri-api-plan.md`). Internal `agriBack/` Django module
  rename is deferred to Phase 9.

## What's in this PR

| File | Change | |---|---| | `docker-compose.yml` | image `agriback:latest` → `agri-api:latest`;
  containers renamed (`agrybackend` → `agri-api-web`, `celery-worker` → `agri-api-worker`,
  `celery-beat` → `agri-api-beat`, `agryserver` → `agri-bridge`) | | `Devops/server/server.js` |
  `PY_HOST` default matches new web container name | | `back/pyproject.toml` | `name = "agri-api"` |
  | `back/uv.lock` | refreshed via `uv lock` | | `docs/flows/{alerts,data-ingestion}.md` |
  references the new container names | | `back/agriBack/settings.py` | `ALLOWED_HOSTS` default
  contains `agri-api-web` | | **New:** `CLAUDE.md` + `.claudeignore` + `docs/INDEX.md` | from `cto
  init`; Claude Code session entry point (`.claude/*.md` stays out per `.gitignore`) |

## After merge — operator steps

The repo rename + local folder rename happen **after** this PR merges, on a separate ticket-free
  operator step (no code changes):

```bash gh repo rename agri-api -R AgriLogy/agrilogy-back cd ~/agrilogy && mv agrilogy-back agri-api
  cd agri-api && git remote set-url origin git@github.com:AgriLogy/agri-api.git make down && make
  build && make up ```

## Smoke test (post-merge + rename)

```bash curl -sf -o /dev/null -w "%{http_code}\n" http://localhost:8000/admin/login/ curl -sf -o
  /dev/null -w "%{http_code}\n" http://localhost:8000/swagger/ # both must be < 500 ```

A Postman collection lives in this PR under `docs/postman/agri-api.postman_collection.json` (added
  next commit).

## Out of scope

- Internal `agriBack/` Django module rename → Phase 9 - `agry_admin` / `agrydata_db` / `agrydata`
  settings defaults (dead since Supabase took over) → separate cleanup - Sibling repo renames
  (`agrilogy-db`, `agrilogy-front`, `agrilogy-landing-page`) → separate Phase 0 sub-issues

## Conflicts to watch

PR #47 (`fix/compose-email-overrides`) also touches `docker-compose.yml` (deletes the
  EMAIL_HOST/EMAIL_PORT lines). Whichever lands second will need a one-line rebase.

- **release**: 1.7.2 [skip ci]
  ([`3d75c9a`](https://github.com/AgriLogy/agri-api/commit/3d75c9a5dc481454fee8c88150067e3704afc7b1))

## [1.7.2](https://github.com/AgriLogy/agrilogy-back/compare/v1.7.1...v1.7.2) (2026-05-28)


## v1.7.1 (2026-05-22)

### Bug Fixes

- **bridge**: Structured logs with client, status, and error body
  ([#45](https://github.com/AgriLogy/agri-api/pull/45),
  [`a07a719`](https://github.com/AgriLogy/agri-api/commit/a07a7196d06b637db5db1d5fcd62f8ef0a1e64b7))

Closes #44

## Summary - Replace `console.log("true"/"false")` with one structured line per forward, including
  `client`, `keys` count, and either the Django response status/body (ok) or the rejection error
  message including the upstream status code and body (fail). - Failures now go through
  `console.error` so docker log streams classify them correctly. - `postToPython` resolves with `{
  statusCode, body }` so callers can surface the status. Internal API change only — the bridge
  response to the device is unchanged.

## Why We just spent an investigation triangulating a soil-data outage that the bridge could have
  told us about in a single line: every soil POST was being rejected by Django with `400 "User not
  found for client 'Router01'"` and the bridge was just logging `false`.

## Test plan - [ ] Rebuild `agryserver` (`docker compose up -d --build agryserver`) on staging. - [
  ] `docker logs -f agryserver` — confirm one structured line per request. - [ ] Provoke a failure
  (e.g., temporarily set PY_PATH to a nonexistent route) and confirm the FAIL line carries the
  upstream status + body. - [ ] Promote `main → alpha` at the next release window.

- **ingest**: Isolate alert dispatch so a failure can't drop sensor writes
  ([#43](https://github.com/AgriLogy/agri-api/pull/43),
  [`e2eecea`](https://github.com/AgriLogy/agri-api/commit/e2eeceaec9acefd23f5799fa4f8aa660e9e65d17))

Closes #42

## Summary - Wrap `dispatch_alerts_for_reading` in `try/except` inside `WeatherIngestAPIView.post`.
  The sensor row is already persisted by the time we reach the dispatch — an exception there now
  becomes a logged warning instead of aborting the loop and silently dropping every remaining key in
  the payload. - We hit this exact failure mode on prod when the pending
  `0059_alert_last_emailed_at` migration meant the alerts query crashed with
  `psycopg.errors.UndefinedColumn`. Only the first key per request landed; soil data sat frozen for
  days behind a "weather is fine" symptom.

## Test plan - [ ] Local: stub `dispatch_alerts_for_reading` to raise; confirm every key in the
  payload still writes a sensor row and the response is still `201`. - [ ] Confirm
  `logger.exception` appears with the failing `sensor_key` / `user` / `zone`. - [ ] Staging: deploy,
  push a multi-key payload, confirm all rows land. - [ ] Promote `main → alpha` at the next release
  window.

## Notes - Behaviour change is intentionally narrow: only swallows alert-side exceptions.
  Sensor-write errors still bubble up (would be a real ingest bug we'd want to know about).

- **tasks**: Drop unused self arg breaking simulate_sensor_ingest
  ([#41](https://github.com/AgriLogy/agri-api/pull/41),
  [`d08762a`](https://github.com/AgriLogy/agri-api/commit/d08762a74b8e7b86efd19dc8934077ca6a272492))

Closes #40

## Summary - `simulate_sensor_ingest` was decorated with `@shared_task` (not `bind=True`) but its
  signature took `self`, so every beat tick raised `TypeError` and the soil-sensor simulator never
  ran. Soil tables have been frozen at `2026-05-19 23:45 UTC` since. - Drop the unused `self`
  parameter — it is never referenced in the body, so this is the minimal correct fix.

## Test plan - [ ] Merge to `main`, deploy to staging, observe celery-beat logs no longer raise
  `SchedulingError`. - [ ] Confirm `SELECT MAX(timestamp) FROM analytics_soilmoisturemedium;`
  advances within a couple of beat ticks. - [ ] Promote `main → alpha` for the production fix at the
  next release window.

### Chores

- **release**: 1.7.1 [skip ci]
  ([`2ab89e9`](https://github.com/AgriLogy/agri-api/commit/2ab89e935b6ff37269229a22f5daba595343c119))

## [1.7.1](https://github.com/AgriLogy/agrilogy-back/compare/v1.7.0...v1.7.1) (2026-05-22)

### Bug Fixes

* **bridge:** structured logs with client, status, and error body
  ([#45](https://github.com/AgriLogy/agrilogy-back/issues/45))
  ([a07a719](https://github.com/AgriLogy/agrilogy-back/commit/a07a7196d06b637db5db1d5fcd62f8ef0a1e64b7)),
  closes [#44](https://github.com/AgriLogy/agrilogy-back/issues/44) * **ingest:** isolate alert
  dispatch so a failure can't drop sensor writes
  ([#43](https://github.com/AgriLogy/agrilogy-back/issues/43))
  ([e2eecea](https://github.com/AgriLogy/agrilogy-back/commit/e2eeceaec9acefd23f5799fa4f8aa660e9e65d17)),
  closes [#42](https://github.com/AgriLogy/agrilogy-back/issues/42) * **tasks:** drop unused self
  arg breaking simulate_sensor_ingest ([#41](https://github.com/AgriLogy/agrilogy-back/issues/41))
  ([d08762a](https://github.com/AgriLogy/agrilogy-back/commit/d08762a74b8e7b86efd19dc8934077ca6a272492)),
  closes [#40](https://github.com/AgriLogy/agrilogy-back/issues/40)


## v1.7.0 (2026-05-20)

### Chores

- **release**: 1.7.0 [skip ci]
  ([`128be66`](https://github.com/AgriLogy/agri-api/commit/128be66a973f17c77698cd96da42e444408e63a3))

# [1.7.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.6.0...v1.7.0) (2026-05-20)

### Features

* **ingest:** accept the full sensor catalogue via registry-driven view
  ([#39](https://github.com/AgriLogy/agrilogy-back/issues/39))
  ([b0d47ec](https://github.com/AgriLogy/agrilogy-back/commit/b0d47ec055be845d22882dcc37a8b7c0466bc951)),
  closes [#38](https://github.com/AgriLogy/agrilogy-back/issues/38)

### Features

- **ingest**: Accept the full sensor catalogue via registry-driven view
  ([#39](https://github.com/AgriLogy/agri-api/pull/39),
  [`b0d47ec`](https://github.com/AgriLogy/agri-api/commit/b0d47ec055be845d22882dcc37a8b7c0466bc951))

Closes #38

## Summary - `WeatherIngestAPIView` is now driven by `SENSOR_KEY_REGISTRY`: any payload key in the
  registry with a non-None value is persisted to its model and handed to
  `dispatch_alerts_for_reading`. Adding a new sensor is now a one-line registry edit, not a view
  change. - The registry gains the 12 keys that were missing relative to `analytics.models`:
  `wind_direction`, `ec_soil_low/_medium/_high`, `soil_moisture_low/_high`,
  `soil_temperature_low/_high`, `ec_salinity`, `multi_depth_soil_moisture`, `water_level`,
  `et0_weather`, `et0_calculated`. Canonical pH key is `ph_soil` (matches the device's
  snake-case-of-model-name); `soil_ph` and `et0` stay as back-compat aliases. - `NPK` is
  intentionally skipped (`NpkSensor` has three value fields, needs per-field registry routing —
  separate ticket). - Knock-on: alerts on every newly accepted sensor key fire on ingest through the
  same dispatcher shipped in #36.

## Tests 9 new tests in `analytics/tests/test_alerts.py::IngestViewFullCatalogueTests`: - soil keys
  land in their models - `wind_direction` and `water_level` now land - unknown keys silently dropped
  (vs. `all_metrics_none` when everything is unknown) - `npk` payload is skipped without erroring -
  both `ph_soil` and `soil_ph` resolve to `PhSoil` - an alert on `soil_moisture_low` fires
  end-to-end on ingest

Full suite **131/131 green**.

## Docs - `docs/flows/data-ingestion.md`: §3 rewritten to describe the registry loop; sequence
  diagram + "Adding a new sensor" entry updated; obsolete `METRIC_KEYS` references removed. -
  `docs/flows/alerts.md`: known-issues entry "Push only fires for the 6 weather metrics today"
  replaced with the (narrower) NPK exclusion.

## Test plan - [ ] CI green - [ ] Existing alerts on `temperature_weather`, `wind_speed`, etc. still
  fire (no behaviour regression for keys already in the registry) - [ ] Smoke test: post
  `{"client":"Router01","ec_soil_low":160}` to `:9090` in dev, verify an `ECSoilLow` row lands


## v1.6.0 (2026-05-20)

### Chores

- **release**: 1.6.0 [skip ci]
  ([`c06637b`](https://github.com/AgriLogy/agri-api/commit/c06637bcc5e485012f0d4d5294bd08ffda63d155))

# [1.6.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.5.1...v1.6.0) (2026-05-20)

### Features

* **alerts:** email on ingest when an active alert fires, throttled per sensor
  ([#35](https://github.com/AgriLogy/agrilogy-back/issues/35))
  ([cc7a0bc](https://github.com/AgriLogy/agrilogy-back/commit/cc7a0bc6c156e2006009dcde07b53485d99b7ae8)),
  closes [#36](https://github.com/AgriLogy/agrilogy-back/issues/36)

### Features

- **alerts**: Email on ingest when an active alert fires, throttled per sensor
  ([#35](https://github.com/AgriLogy/agri-api/pull/35),
  [`cc7a0bc`](https://github.com/AgriLogy/agri-api/commit/cc7a0bc6c156e2006009dcde07b53485d99b7ae8))

Closes #36

## Summary Fixes the gap flagged in `docs/flows/alerts.md` — alerts were stored but never delivered.
  Now, every new sensor row written via the ingest path triggers a per-alert evaluation; matching
  rules fan out a single email via a Celery task, throttled by a per-sensor grace period.

## How it works - New `Alert.last_emailed_at` column (migration `0059`) acts as the rate-limit
  cursor — distinct from `last_triggered_at` which keeps its "first-ever fire" semantics for chart
  overlays. - `dispatch_alerts_for_reading(sensor_key, zone, user, value, timestamp)` in
  `back/analytics/alerts.py` is called from `WeatherIngestAPIView` after each metric write. It
  evaluates every active alert for `(user, sensor_key, zone)` and, for each rule that fires, runs a
  **conditional UPDATE** on `last_emailed_at` — only the row that wins the race enqueues a
  `send_alert_email` Celery task. Two simultaneous readings cannot send two emails. -
  `send_alert_email` (in `back/agriBack/tasks.py`) is defensive: bails out cleanly on
  `alert_missing`, `alert_inactive`, `no_recipient`, `smtp_error`.

## Grace periods (defaults) Configurable in `back/agriBack/settings.py` — `ALERT_GRACE_PERIODS` (per
  `sensor_key`) + `DEFAULT_ALERT_GRACE_PERIOD` fallback (1800 s). Defaults:

| Sensor family | Default grace | |--------------------------|--------------:| | Water (flow, level,
  pressure, EC, pH) | 5 min | | Wind (speed, direction) | 15 min | | Weather (T, RH, P, solar,
  precip) | 30 min | | Leaf / electricity | 30 min | | ET0 / soil moisture | 1 h | | Soil
  temperature / soil chemistry | 2 h | | NPK | 4 h | | Fruit size / large fruit | 6 h |

Override per-env in settings or via the dict at runtime.

## Tests 22 new tests in `analytics/tests/test_alerts.py`: - `DispatchAlertsForReadingTests` — value
  below/above threshold, grace gate, resume after grace, inactive alert, zone-scoped vs user-wide,
  unknown sensor_key, None value - `GracePeriodLookupTests` — `ALERT_GRACE_PERIODS` lookup +
  `DEFAULT_ALERT_GRACE_PERIOD` fallback + `override_settings` - `IngestViewAlertDispatchTests` —
  POST `/api/sensors/weather/ingest/` with metric crossing/under threshold, fan-out across multiple
  metrics in one POST - `SendAlertEmailTaskTests` — defensive branches
  (missing/inactive/no-recipient) + happy path with mail.outbox assertion

Full suite **122/122** green via `make test`.

## Docs - `docs/flows/alerts.md` gets a new §10 "Push-on-ingest dispatch" with sequence diagram +
  grace-period table + race-semantics note. The TL;DR is updated to describe both evaluation paths
  (push on write, lazy on read). The obsolete "No alert → notification dispatch" entry is removed
  from Known issues. - `docs/flows/data-ingestion.md` mentions the alert fan-out in the
  critical-path sequence diagram and points at the alerts doc for the details.

## Notes - The simulator (`simulate_sensor_ingest`) is intentionally NOT wired — synthetic readings
  shouldn't email anyone. - Only the 6 weather metrics fire today because that's the only live
  ingest path. Soil/fruit/leaf/NPK/water alerts will start firing the moment those sensors get a
  device-facing endpoint.

## Test plan - [ ] CI green - [ ] Manually post above-threshold to `:9090` against a dev DB with an
  active alert; verify mailpit catches one email and a second POST within the grace window does NOT
  - [ ] After grace window passes, the next above-threshold POST emails again


## v1.5.1 (2026-05-20)

### Chores

- **release**: 1.5.1 [skip ci]
  ([`01f03c9`](https://github.com/AgriLogy/agri-api/commit/01f03c9ee6b9bda5f4c7929a261a0c65e6f7c07c))

## [1.5.1](https://github.com/AgriLogy/agrilogy-back/compare/v1.5.0...v1.5.1) (2026-05-20)

### Documentation

- **flows**: End-to-end architecture maps for ingestion, notifications, alerts
  ([`672a3d0`](https://github.com/AgriLogy/agri-api/commit/672a3d0a9986fcd6d6df39d9bc95de33b00d3233))

Three reference documents under docs/flows/ capturing how a sensor reading travels from device to
  DB, how an email notification is composed and sent, and how alert rules are evaluated and
  surfaced.

Each doc carries a TL;DR, a graph mermaid for the high-level architecture, one or more
  sequenceDiagram blocks for the critical path, and a per-component breakdown with file:line
  citations. Known issues and footguns (auth-less ingest, silent forward failure, mount path
  mismatch, missing alert→notification fan-out, single-zone snapshot, etc.) are listed explicitly so
  the team has a written record.

The alerts doc also documents the adjacent manager-affirmation workflow with its state machine,
  called out explicitly as NOT alert-related so future readers don't conflate the two.

- **flows**: End-to-end architecture maps for ingestion, notifications, alerts
  ([#33](https://github.com/AgriLogy/agri-api/pull/33),
  [`920c4bd`](https://github.com/AgriLogy/agri-api/commit/920c4bd882c7e3622d8ea1b27a182f065ffbbf42))


## v1.5.0 (2026-05-20)

### Chores

- **release**: 1.5.0 [skip ci]
  ([`d999826`](https://github.com/AgriLogy/agri-api/commit/d999826ce5a26b0df76f6636bdea194e35d01359))

# [1.5.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.4.2...v1.5.0) (2026-05-20)

### Features

* **agronomy:** Dr/RAW-based irrigation decision per agronomist spec
  ([6b72c58](https://github.com/AgriLogy/agrilogy-back/commit/6b72c58d8c105ac6b28c29d34a5eff456f4015d8))
  * **agronomy:** Dr/RAW-based irrigation decision per agronomist spec
  ([#32](https://github.com/AgriLogy/agrilogy-back/issues/32))
  ([67d8171](https://github.com/AgriLogy/agrilogy-back/commit/67d8171b4a4ef917d755c80ac6885fcb737a8a6c))

### Features

- **agronomy**: Dr/raw-based irrigation decision per agronomist spec
  ([`6b72c58`](https://github.com/AgriLogy/agri-api/commit/6b72c58d8c105ac6b28c29d34a5eff456f4015d8))

Implements §3–§4 of the agronomist's revised spec (Dr daily depletion, RAW threshold, effective
  rainfall, rain-forecast branch with suspension vs. complementary irrigation, missed-irrigation
  catch-up helper) as pure functions in agronomy.py, plus the §2.2 crop-stage → Zr/TAW/RAW table as
  a module constant.

field_snapshot now accepts dr_today_mm + precipitation_forecast_mm and routes through the new
  irrigation_decision_dr when zone params allow it; falls back to today's threshold rule when Dr
  isn't supplied, keeping every existing caller behaviour-stable.

Deferred to follow-up tickets (tracked in the umbrella issue): Dr persistence + Celery rollover,
  dynamic crop-stage detection, notification template rewrite, valve API, α calibration, AI module,
  weather-forecast API integration.

29 new tests across WaterBalanceMathTests, IrrigationDecisionDrTests, and
  FieldSnapshotDrIntegrationTests. Full suite 103/103 green.

- **agronomy**: Dr/raw-based irrigation decision per agronomist spec
  ([#32](https://github.com/AgriLogy/agri-api/pull/32),
  [`67d8171`](https://github.com/AgriLogy/agri-api/commit/67d8171b4a4ef917d755c80ac6885fcb737a8a6c))


## v1.4.2 (2026-05-20)

### Bug Fixes

- **analytics**: Serialize sensor timestamps with full ISO 8601 precision
  ([`661b34d`](https://github.com/AgriLogy/agri-api/commit/661b34d061ffe79ece7009500dee2979df59dc0c))

The previous date-only format collapsed every reading in a given day to the same string, so the
  front-end Set-based dedup rendered ~2000 daily samples as one chart point. Switch to ISO 8601 UTC
  with second precision so each reading is a distinct X-value.

Closes #29

- **notifications**: Space numeric values from units (°C, %) in email body
  ([`b638506`](https://github.com/AgriLogy/agri-api/commit/b63850632ba8cb70e851c800c13c691cd8119ff6))

Aligns email-body formatting with the typographic style already used for mm / µS/cm / mg/L so every
  numeric reading has a visible unit.

Closes #30

### Chores

- **release**: 1.4.2 [skip ci]
  ([`efbf7f3`](https://github.com/AgriLogy/agri-api/commit/efbf7f3ef881cd497546aa13e79d1e28c431924c))

## [1.4.2](https://github.com/AgriLogy/agrilogy-back/compare/v1.4.1...v1.4.2) (2026-05-20)

### Bug Fixes

* **analytics:** serialize sensor timestamps with full ISO 8601 precision
  ([661b34d](https://github.com/AgriLogy/agrilogy-back/commit/661b34d061ffe79ece7009500dee2979df59dc0c)),
  closes [#29](https://github.com/AgriLogy/agrilogy-back/issues/29) * **notifications:** space
  numeric values from units (°C, %) in email body
  ([b638506](https://github.com/AgriLogy/agrilogy-back/commit/b63850632ba8cb70e851c800c13c691cd8119ff6)),
  closes [#30](https://github.com/AgriLogy/agrilogy-back/issues/30)


## v1.4.1 (2026-05-17)

### Chores

- **db**: Host Postgres on Supabase and freeze schema to agrilogy-db
  ([`17bc568`](https://github.com/AgriLogy/agri-api/commit/17bc568c64e4b211dcb12b46dbd5098829524e23))

## Summary

- Local Docker stack no longer ships a Postgres container — moved to a hosted **Supabase** project
  (Session pooler, eu-central-1). - Schema-of-record relocated to the new sibling repo
  [**agrilogy-db**](https://github.com/AgriLogy/agrilogy-db), which manages it via **Alembic +
  SQLAlchemy 2.x**. Django no longer auto-migrates on container boot. - Baseline Alembic revision
  (`e46347540b51_baseline_from_django_v57`) is a `pg_dump --schema-only` of the schema as it stood
  at the end of the Django era. Supabase dev was bootstrapped by Django one last time, then stamped
  at the baseline. - Motivation: groundwork for the planned **FastAPI rewrite** — migration history
  outlives the framework swap.

## Files changed

| File | Change | | --- | --- | | `docker-compose.yml` | Removed `agrydata` (Postgres 17) +
  `agryadminer` services + `data_database` volume. Web/worker/beat no longer override `POSTGRES_*`;
  they read from `back/.env`. | | `back/docker-entrypoint.sh` | Removed `run_migrations` from the
  `web` role. New comment points future readers at `agrilogy-db`. | | `back/env-example` | Documents
  the Supabase Session pooler URI pattern instead of a local Postgres. | | `readme.md` | New ⚙️
  Database section. | | `.mcp.json` (new) | Wires `claude` / Cursor / etc. to the Supabase MCP so
  contributors get tooling parity. |

## Deploy-time risk to flag

Merging to `main` triggers `deploy-back.yml`, which SSHes into DigitalOcean. Two things to be aware
  of:

1. **No pending Django migrations exist today**, so the deploy is safe right now. 2. **The
  DigitalOcean prod Postgres is NOT yet tracked by `agrilogy-db`.** Future Django model changes will
  need either the DO→Supabase prod cutover to land first, OR a hand-rolled migration plan against
  the legacy DB. CONTINUE.md captures this.

## Test plan

- [x] Local: `docker compose restart agrybackend` boots with no `Running migrations` line in the
  entrypoint logs. - [x] Local: `GET /admin/login/` returns 200 (Django ↔ Supabase pooler round-trip
  works). - [x] `alembic current` against Supabase dev reports head `e46347540b51`. - [x] Supabase
  Data API (PostgREST) disabled on both dev + prod projects — `GET /rest/v1/...` returns 503 with
  `PGRST002` (PostgREST can't reach the DB). - [ ] Reviewer: pull the branch, run `cd ../agrilogy-db
  && make upgrade-dev` (or `make stamp-dev-head` if your local Supabase dev was bootstrapped by
  Django), then `make up`. Sanity-check the dashboards render.

## Follow-ups (tracked in CONTINUE.md)

- Capture `agrilogy-prod` DB password → `make upgrade-prod` to lay the baseline on prod. -
  DigitalOcean Postgres → Supabase prod cutover (needs maintenance window with real user data). -
  Decide whether to delete the now-frozen `back/<app>/migrations/` directories or leave them as
  historical reference.

- **release**: 1.4.1 [skip ci]
  ([`c059b93`](https://github.com/AgriLogy/agri-api/commit/c059b9382817d521316721f25db2088a75f88752))

## [1.4.1](https://github.com/AgriLogy/agrilogy-back/compare/v1.4.0...v1.4.1) (2026-05-17)


## v1.4.0 (2026-05-14)

### Chores

- **release**: 1.4.0 [skip ci]
  ([`f9aa10e`](https://github.com/AgriLogy/agri-api/commit/f9aa10e4916961888e55c1a5ab9e5cea0f7b8eca))

# [1.4.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.3.1...v1.4.0) (2026-05-14)

### Features

* **admin:** backoffice CRUD + manager affirmation
  ([#20](https://github.com/AgriLogy/agrilogy-back/issues/20))
  ([ade25ad](https://github.com/AgriLogy/agrilogy-back/commit/ade25adea5adc5d50b03641beba61770400ac32a))
  * **admin:** backoffice CRUD endpoints + manager affirmation + tests
  ([df549f2](https://github.com/AgriLogy/agrilogy-back/commit/df549f2553ecfc31b480a85411c1fd2f0e9f217e))

### Features

- **admin**: Backoffice CRUD + manager affirmation
  ([#20](https://github.com/AgriLogy/agri-api/pull/20),
  [`ade25ad`](https://github.com/AgriLogy/agri-api/commit/ade25adea5adc5d50b03641beba61770400ac32a))

- **admin**: Backoffice CRUD endpoints + manager affirmation + tests
  ([`df549f2`](https://github.com/AgriLogy/agri-api/commit/df549f2553ecfc31b480a85411c1fd2f0e9f217e))

Adds a REST-conformant admin surface under /auth/admin/users/* and /api/admin/* covering user CRUD,
  soft-delete, password reset, zone CRUD, zone params, per-user sensor unit preferences, per-user
  alert override, per-user activity timeline, and a dashboard overview. Introduces the manager
  affirmation workflow (user requests a sensitive change → admin approves or rejects).

Conventions:

- DRF generics (ListCreateAPIView / RetrieveUpdateDestroyAPIView) instead of hand-rolled APIView
  gets/puts. - IsAuthenticated + IsAdminUser on every admin route. New IsAdminOrSelf permission for
  shared user-or-admin endpoints. - Hardens the legacy /auth/users/ and /auth/modify-user/ (were
  AllowAny) to IsAdminUser. - Explicit serializer fields + read_only_fields (no __all__ on admin
  write paths). validate_* hooks for password strength, email uniqueness, lat/lon ranges, threshold
  ranges, FC >= WP. - DRF exceptions (NotFound, ValidationError) instead of bespoke Response
  status=400 envelopes. - logging.getLogger; no print(). - Indexes on -date_joined + is_active.

Schema changes:

- CustomUser.date_joined (default=timezone.now) + Meta.ordering + two new indexes. - New
  UserSensorUnitPreference (user FK, sensor_key, unit, unique). - New ManagerAffirmation
  (requested_by, action, payload JSON, status, decided_by, decided_at, decision_note).

Tests:

- 82 new pytest-django tests under CustomUser/tests/ and analytics/tests/ — auth (401), permission
  (403), happy path, cross-user isolation, validation, 404s, idempotency, DB side effects. - 165/165
  pass locally (82 new + 83 legacy unaffected).

CI:

- Backend CI step swaps `python manage.py test` for `uv run pytest CustomUser/ analytics/`. The
  Django runner skips pytest-fixture tests; pytest-django is a strict superset and still picks up
  the existing django.test.TestCase suites.

Validated statically only (ruff check, ruff format --check, manage.py check, makemigrations --check
  --dry-run, pytest). No docker / postgres run yet — the dockerised stack is on the reviewer to
  exercise locally.


## v1.3.1 (2026-05-12)

### Bug Fixes

- **agronomy**: Apply 2026-05-10 review corrections to ET0 hourly math
  ([`91b2e97`](https://github.com/AgriLogy/agri-api/commit/91b2e970a16331f8144b7efc9e4eaaaf23f930ff))

Apply six clauses from the agronomist's review (Correction ET0 10.05.26 + final et0) on top of the
  consolidated agronomy module:

- RH clamped to [1, 100] % (a stuck-at-0 sensor would otherwise inflate VPD and ET0; 1 % keeps the
  formula in physical range) - Rs/Rso ratio clamped to [0.3, 1.0] with the 0.3 floor used at night
  (Rso ~= 0), so the cloudiness function 1.35*ratio - 0.35 stays positive instead of producing a
  negative Rnl - Hourly Ra now wired in via lat/lon: solar_time_correction_hours and
  extraterrestrial_radiation_hourly_mjm2h follow FAO-56 Annex 2 with the longitude term derived
  under our east-positive sign convention (documented in the docstring) and the equation of time as
  the seasonal correction - Wind projected to 2 m via FAO eq. 47, identity when the sensor is
  already at 2 m - Daytime detected from Rs > 0 (not Rn > 0), so a brief Rn dip on cool humid hours
  does not flip Cd/G into the daytime regime - Defensive CLOUD_FACTOR_MIN floor on 1.35*ratio - 0.35

compute_et0_for_zone now reads CustomUser.latitude/longitude and feeds the hour-midpoint in
  Africa/Casablanca local civil time into the Ra computation; zones without lat/lon fall back to the
  0.75 heuristic so nothing regresses for legacy data.

Tests: PureMathTests updated for the new RH clamp; new AgronomistReviewCorrectionsTests class adds
  one named test per clause so a future refactor that drops one surfaces as a single attributable
  failure. Full backend suite: 83 passed (70 prior + 13 new).

- **agronomy**: Apply 2026-05-10 review corrections to ET0 hourly math
  ([#19](https://github.com/AgriLogy/agri-api/pull/19),
  [`2ac9ad9`](https://github.com/AgriLogy/agri-api/commit/2ac9ad9430138f05d35e1ce587354cbf8ae9e750))

### Chores

- **release**: 1.3.1 [skip ci]
  ([`8bdc031`](https://github.com/AgriLogy/agri-api/commit/8bdc0310e7bdaa1e4cd71035943e1a5e6e9b60cf))

## [1.3.1](https://github.com/AgriLogy/agrilogy-back/compare/v1.3.0...v1.3.1) (2026-05-12)

### Bug Fixes

* **agronomy:** apply 2026-05-10 review corrections to ET0 hourly math
  ([91b2e97](https://github.com/AgriLogy/agrilogy-back/commit/91b2e970a16331f8144b7efc9e4eaaaf23f930ff))
  * **agronomy:** apply 2026-05-10 review corrections to ET0 hourly math
  ([#19](https://github.com/AgriLogy/agrilogy-back/issues/19))
  ([2ac9ad9](https://github.com/AgriLogy/agrilogy-back/commit/2ac9ad9430138f05d35e1ce587354cbf8ae9e750))


## v1.3.0 (2026-05-11)

### Chores

- Ignore .claude/ session-state directory
  ([`765ea7c`](https://github.com/AgriLogy/agri-api/commit/765ea7ce85b866b9ca34a139d556853698c44995))

- **release**: 1.3.0 [skip ci]
  ([`2aeae6b`](https://github.com/AgriLogy/agri-api/commit/2aeae6b93878b6e6d16d5b30194c1a57e4bb95a9))

# [1.3.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.2.0...v1.3.0) (2026-05-11)

### Features

* **agronomy:** consolidate ET0 + irrigation math into one expert-own…
  ([#14](https://github.com/AgriLogy/agrilogy-back/issues/14))
  ([7956830](https://github.com/AgriLogy/agrilogy-back/commit/79568306123bb42f32499fe519e421b3ca0465db))
  * **agronomy:** consolidate ET0 + irrigation math into one expert-owned module
  ([4bb0280](https://github.com/AgriLogy/agrilogy-back/commit/4bb0280cef0e71450c956a984ab2f26157560a6d)),
  closes [hi#level](https://github.com/hi/issues/level) * **alerts:** plug-and-play alert module +
  dev seed scripts + containerised stack
  ([c8855fc](https://github.com/AgriLogy/agrilogy-back/commit/c8855fc46711841ad2d000548d0d7e9669785e4e))

### Features

- **agronomy**: Consolidate ET0 + irrigation math into one expert-owned module
  ([`4bb0280`](https://github.com/AgriLogy/agri-api/commit/4bb0280cef0e71450c956a984ab2f26157560a6d))

Until now the FAO-56 Penman-Monteith implementation lived inline in agriBack/tasks.py and the
  notification email body used hardcoded fictional numbers. This split is wrong on two axes: an
  agronomist who wants to refine the math has to hunt through Celery glue code, and the email never
  showed the real ET0 the system was already computing.

agriBack/agronomy.py now owns: 1. Physical constants (albedo, Stefan-Boltzmann) 2. Pure math helpers
  (es, slope, gamma, Rn, G, Penman-Monteith hourly) 3. Sensor aggregation helpers 4. Two high-level
  entry points: - compute_et0_for_zone(zone, end=None) — pure (no DB writes); used by the Celery
  hourly persister. - field_snapshot(user) — returns the dict consumed by the notification email.
  THIS is the function an agronomy expert is expected to evolve. TODO(expert) markers flag stubs
  (zone selection across multi-zone users, Kc lookup, irrigation rule).

Bug fix found via the new tests: the legacy SIGMA constant was the DAILY FAO Stefan-Boltzmann value
  (4.903e-9) but used in an HOURLY formula. That overestimated longwave radiation by 24×, drove Rn
  negative, and silently clamped ET0 to 0 even at midday. Hourly value is 2.043e-10. Test cases
  guard against regressing.

agriBack/tasks.py — compute_et0_vpd_hourly is now a 25-line wrapper that calls compute_et0_for_zone
  and persists Et0Calculated + VPDWeather. No physics duplicated anywhere.

CustomUser/notification_helper.py — perform_calculations now consumes field_snapshot(user) and
  renders a French email body with real sensor-driven numbers (or “—” when data is missing). All
  hardcoded soil moisture / temp / NPK / irrigation strings removed.

Tests: 15 new TestCases in analytics/tests/test_agronomy.py covering both pure math (FAO reference
  values, sign / monotonicity invariants, night vs. afternoon ET0 magnitudes) and the DB-aware entry
  points (missing-input handling, no-write guarantee, decision branches in field_snapshot). 31/31
  green via make test.

- **agronomy**: Consolidate ET0 + irrigation math into one expert-own…
  ([#14](https://github.com/AgriLogy/agri-api/pull/14),
  [`7956830`](https://github.com/AgriLogy/agri-api/commit/79568306123bb42f32499fe519e421b3ca0465db))

- **alerts**: Plug-and-play alert module + dev seed scripts + containerised stack
  ([`c8855fc`](https://github.com/AgriLogy/agri-api/commit/c8855fc46711841ad2d000548d0d7e9669785e4e))

Adds a domain-agnostic alert engine (CRUD + evaluator + suggestion API) that the front-end can
  attach to any chart, plus the developer ergonomics needed to spin the whole stack up via docker
  compose with realistic data on first boot.

## Plug-and-play alert module — `back/analytics/alerts.py`

- `SENSOR_KEY_REGISTRY` (21 keys) maps stable string identifiers to ORM models, units, French
  labels, and the legacy `type` enum. - Pure helpers: `evaluate(condition, threshold, value)`,
  `evaluate_alert(alert, value)`, `latest_value_for(alert)`. - `recent_triggers_for_user(user,
  sensor_key=, zone_id=)` — fan-out used by the `/api/alerts/for-graph/` overlay endpoint. Stamps
  `last_triggered_at` on first observation. - `suggest_alert(user, sensor_key=, zone_id=)` — returns
  a prefilled payload (name, description, condition, condition_nbr=mean, unit, label, type,
  sample_size, is_active) so the front can drop users into a half-completed create form.

### Endpoints

| Method | Path | Purpose | | --- | --- | --- | | GET / POST | `/api/alert/` | list / create | | GET
  / PATCH / PUT / DELETE | `/api/alert/<pk>/` | single-alert ops | | GET |
  `/api/alerts/for-graph/?sensor_key=&zone_id=` | overlay annotations | | GET |
  `/api/alerts/sensor-keys/` | registry exposure | | GET |
  `/api/alerts/suggest/?sensor_key=&zone_id=` | mean-based prefill |

### Model migration (0057)

`Alert` gains: `sensor_key` (CharField), `zone` (nullable FK), `is_active`, `last_triggered_at`,
  `created_at`, `updated_at`. `condition_nbr` widened to Decimal(10, 2). `description` defaults
  blank. Backwards-compatible with existing rows.

### Auto-graph activation

`@receiver(post_save, sender=Zone)` now creates `ActiveGraph`, `GraphName`, `SensorColor` rows on
  every new zone, so dashboards render every chart out of the box without manual `/admin/` toggles.

## Dev seed scripts (gated by env vars; default ON in dev)

- `back/seed_dev_users.py` — idempotent, gated by `SEED_DEV_USERS`. Creates `user1` / `@Agrogo321`
  and `contact-agrilogy` / `pw-dev`, each with `zone de marichage 1` (1 750 m², critical moisture 18
  %) + the `ActiveGraph` / `GraphName` / `SensorColor` rows. - `back/seed_dev_data.py` — idempotent,
  gated by `SEED_DEV_DATA`. Generates 7 days × 15-min cadence of realistic readings across 34 sensor
  models per zone (~22 800 rows / zone). Reuses the season-aware synth helpers from
  `agriBack.tasks::simulate_sensor_ingest` so the values are bounded the same way the live simulator
  produces them. Skips zones that already have meaningful history (last 24 h).

## Containerised stack — `docker-compose.yml` + `back/Dockerfile` + `back/docker-entrypoint.sh`

Single `agriback:latest` image specialised by role (`web` / `worker` / `beat` / `shell`) via the new
  `docker-entrypoint.sh` dispatcher. Health-gated dependency graph (postgres → redis → web → worker
  / beat). The `web` role runs `migrate` + `seed_dev_users.py` + `seed_dev_data.py` before the dev
  server, so a fresh `docker compose up` lands on a fully populated dashboard.

Other services unchanged (postgres 17, redis 7, mailpit, adminer, agryserver Node bridge) but all
  gated behind health checks now.

## Tests — `back/analytics/tests/`

- `test_alerts.py` (30 tests) covering pure threshold predicates, registry resolution, evaluator vs
  ORM rows, CRUD endpoints, suggest endpoint, and multi-user isolation. -
  `test_notification_helper.py` (9 tests) pinning `should_notify` cadence, `_format_message`
  rendering & missing-value path, `perform_calculations` mock. - 70 / 70 tests pass: `docker exec
  agrybackend python manage.py test analytics.tests`.


## v1.2.0 (2026-05-08)

### Chores

- **release**: 1.2.0 [skip ci]
  ([`dc20bcf`](https://github.com/AgriLogy/agri-api/commit/dc20bcf1d6a4a0ff983ab349fc1660e26d787267))

# [1.2.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.1.0...v1.2.0) (2026-05-08)

### Features

* deliver notification emails end-to-end (smtp + endpoints + tests)
  ([66106e8](https://github.com/AgriLogy/agrilogy-back/commit/66106e8a29d4ce9f637539f4601aef87f74a890e))
  * deliver notification emails end-to-end (smtp + endpoints + tests)
  ([#11](https://github.com/AgriLogy/agrilogy-back/issues/11))
  ([a5590e6](https://github.com/AgriLogy/agrilogy-back/commit/a5590e6205765e3fc47f6192b1fa880faa8bb0f7))

### Features

- Deliver notification emails end-to-end (smtp + endpoints + tests)
  ([`66106e8`](https://github.com/AgriLogy/agri-api/commit/66106e8a29d4ce9f637539f4601aef87f74a890e))

Wires up the email notification flow that the frontend was already calling into. Three
  previously-broken paths now actually deliver:

- GET /auth/send-notification/ — was sending to two hardcoded gmail addresses; now sends to
  request.user.email and bumps last_notified. Returns 400 cleanly when the user has no email on
  file. - POST /api/zone-notification-outbound/ — endpoint did not exist; the frontend's
  dispatchZoneNotificationOutbound was silently swallowing 404s. Now sends a confirmation email when
  channels.email is true, honours an optional contactEmail override, no-ops on other channels. - GET
  /api/notifications-and-alerts/ — endpoint did not exist; called by NotificationsMain,
  NavbarNotificationsButton, and the bell-count hook. Returns the user's persisted Notification rows
  in the shape the frontend already expects. - agriBack.tasks.send_periodic_notifications — was a
  stub sending one static message to test@example.com. Now iterates active users with email, gates
  each on should_notify(user), sends via the configured backend, and bumps last_notified.

Settings: EMAIL_BACKEND now respects an env override even in DEBUG so Mailpit testing works without
  flipping DEBUG. EMAIL_HOST / port / TLS are read unconditionally. DEFAULT_FROM_EMAIL falls back to
  a sensible local placeholder.

Model: adds notify_every (PositiveSmallIntegerField default=4 hours) + last_notified (nullable
  DateTimeField) to CustomUser, both already referenced by notification_helper.should_notify(user).
  One migration.

Drive-by fix: removed a pre-existing @receiver(post_save, sender=Notification) decorator that was
  attached to the Et0Weather model class instead of a function. Dormant until something actually
  called Notification.objects.create() — which the test suite now does.

Local SMTP: Mailpit is added to docker-compose (UI :8025, SMTP :1025). README documents the
  verification flow. env-example documents the EMAIL_* knobs.

Tests: 16 new TestCases in back/analytics/tests/test_email_notifications.py covering all four paths
  plus auth + cadence guards. Wired into CI (uv run python manage.py test) and `make test`.

Bundled: back/uv.lock (no new deps; the tooling PR merged before the lockfile commit landed).

- Deliver notification emails end-to-end (smtp + endpoints + tests)
  ([#11](https://github.com/AgriLogy/agri-api/pull/11),
  [`a5590e6`](https://github.com/AgriLogy/agri-api/commit/a5590e6205765e3fc47f6192b1fa880faa8bb0f7))


## v1.1.0 (2026-05-07)

### Bug Fixes

- **deps**: Drop readme path that escapes the project tree
  ([`9fab887`](https://github.com/AgriLogy/agri-api/commit/9fab887a0346acd12e54b38326d91aca3b97fa56))

uv resolves PEP 621 readme paths relative to pyproject.toml; pointing at `../readme.md` from back/
  takes us outside the project root and some PEP 621 validators reject that. The README isn't part
  of the package build anyway since [tool.uv] package = false.

### Chores

- Uv and developer tooling ([#9](https://github.com/AgriLogy/agri-api/pull/9),
  [`54aee58`](https://github.com/AgriLogy/agri-api/commit/54aee5840457438b6600a0d55f1a5eb16f6065f6))

- **release**: 1.1.0 [skip ci]
  ([`23ac181`](https://github.com/AgriLogy/agri-api/commit/23ac181b1fcd07f570a819042f56e6652f30d158))

# [1.1.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.0.0...v1.1.0) (2026-05-07)

### Bug Fixes

* **deps:** drop readme path that escapes the project tree
  ([9fab887](https://github.com/AgriLogy/agrilogy-back/commit/9fab887a0346acd12e54b38326d91aca3b97fa56))

### Features

* **scripts:** add scripts/dev.sh launcher and `make dev` target
  ([fe46d0b](https://github.com/AgriLogy/agrilogy-back/commit/fe46d0ba0e05482a0f265566e7c539cb866620aa))

### Features

- **scripts**: Add scripts/dev.sh launcher and `make dev` target
  ([`fe46d0b`](https://github.com/AgriLogy/agri-api/commit/fe46d0ba0e05482a0f265566e7c539cb866620aa))

One-shot local dev: ensures back/.env exists with safe SQLite defaults, exports the vars, runs uv
  sync + manage.py migrate, then starts the dev server on :8000. Idempotent — re-run anytime. Skip
  steps with SKIP_SYNC=1 / SKIP_MIGRATE=1; override port with PORT=.


## v1.0.0 (2026-05-07)

### Bug Fixes

- Domain name issue
  ([`9d12750`](https://github.com/AgriLogy/agri-api/commit/9d12750e190040bbc2bab8c810e9deecde8aa063))

- Dummy insertion data
  ([`0567885`](https://github.com/AgriLogy/agri-api/commit/056788584a33a2abb7fe6b3054426d34321580f2))

- Env example
  ([`9896566`](https://github.com/AgriLogy/agri-api/commit/98965668f5d1cb66c501fba972eeb6b5051c875e))

- Front domain name
  ([`7e9c879`](https://github.com/AgriLogy/agri-api/commit/7e9c879680d87ad2084127ae90c12af52c160ad0))

- Update the data server
  ([`184186b`](https://github.com/AgriLogy/agri-api/commit/184186b4b3fddf7a5cf91c57759761fbd798b96f))

- User-zone mapping
  ([`9c62202`](https://github.com/AgriLogy/agri-api/commit/9c62202067b8db86e80321a1a47e571d0eba7c6f))

- **ci**: Drop uv cache config until uv.lock is committed
  ([`27a4bab`](https://github.com/AgriLogy/agri-api/commit/27a4babdc463dd39f3eb2d040c52161dae38a231))

setup-uv hard-fails when cache-dependency-glob matches nothing. The lockfile won't exist until
  someone runs `uv sync` with network access and commits the result; until then, run CI without the
  cache layer.

- **ci**: Loosen ruff config to match legacy flake8 leniency
  ([`bb81c15`](https://github.com/AgriLogy/agri-api/commit/bb81c159b92489a6c8d5f33e7179a894081c92e9))

The strict starter rule set (N, UP, B, C4, DJ, PT, RET, SIM, T20) flagged 348 issues in the existing
  codebase, including renames of scientific variables (Rn, ET0, Kc) that are intentionally uppercase
  per the FAO Penman-Monteith convention.

This PR is about installing the tooling, not rewriting the codebase. Drop back to pyflakes-only
  ('F') with the same five F-code ignores the old flake8 used. The strict rule families can be opted
  in file-by-file or after a global cleanup PR.

Also defer 'ruff format --check' until someone runs 'make format' against the whole tree with
  network access and commits the normalized output — otherwise format check would block every PR on
  legacy drift.

### Chores

- Change deploy from back to main
  ([`18dce30`](https://github.com/AgriLogy/agri-api/commit/18dce30b62b47a1d5b086b185abe4663cd549fa3))

- Format backend
  ([`f56ccf3`](https://github.com/AgriLogy/agri-api/commit/f56ccf35f1c814940ce0bce212d7a30520f1ddd3))

- Introduce uv, ruff, semantic-release, and protective git hooks
  ([`47781ab`](https://github.com/AgriLogy/agri-api/commit/47781abf0cd38a7fc272d5dd4c796f4313c0eaf6))

Replaces pip with uv, black/isort/flake8 with ruff, and adds a full release pipeline mirroring the
  frontend's conventions.

Tooling: - pip → uv: back/pyproject.toml owns the project (deps from the previous UTF-16
  requirements.txt re-encoded as UTF-8 + pinned in [project]). Dockerfile uses uv sync. uv.lock will
  be generated on first `make install` and committed to the repo. - black + isort + flake8 → ruff:
  single tool for lint + format + import sort. Config in back/pyproject.toml. - Makefile: bootstrap
  / install / hooks / lint / format / format-check / check / test alongside the existing docker
  compose targets.

Local guards (.githooks/, wired via core.hooksPath): - commit-msg: rejects messages that don't match
  Conventional Commits. - pre-push: refuses pushes to main/master/back/front from a feature branch
  and runs `ruff check` + `ruff format --check` before letting the push through. Override with
  PRE_PUSH_SKIP=1 in genuine emergencies.

CI/CD: - .github/workflows/lint-pr-title.yml — Conventional Commits PR title validator (squash-merge
  uses the PR title as the commit message). - .github/workflows/ci.yml — uv sync → ruff check → ruff
  format → manage.py check on every PR + push to main. - .github/workflows/release.yml —
  semantic-release on push to main: classifies commits, bumps back/pyproject.toml via
  scripts/bump_version.py, regenerates CHANGELOG.md, tags the release. Existing deploy-back.yml
  unchanged — it still picks up the release commit and ships to DigitalOcean.

Branch protection: enable the rule on `main` server-side so direct pushes are blocked at GitHub too
  (local hook is a soft fence).

- **release**: 1.0.0 [skip ci]
  ([`f8191ee`](https://github.com/AgriLogy/agri-api/commit/f8191ee607acc36c8fc3f31e8addf5b3ca055fe9))

# 1.0.0 (2026-05-07)

### Bug Fixes

* front domain name
  ([7e9c879](https://github.com/AgriLogy/agrilogy-back/commit/7e9c879680d87ad2084127ae90c12af52c160ad0))
  * **ci:** drop uv cache config until uv.lock is committed
  ([27a4bab](https://github.com/AgriLogy/agrilogy-back/commit/27a4babdc463dd39f3eb2d040c52161dae38a231))
  * **ci:** loosen ruff config to match legacy flake8 leniency
  ([bb81c15](https://github.com/AgriLogy/agrilogy-back/commit/bb81c159b92489a6c8d5f33e7179a894081c92e9))
  * domain name issue
  ([9d12750](https://github.com/AgriLogy/agrilogy-back/commit/9d12750e190040bbc2bab8c810e9deecde8aa063))
  * dummy insertion data
  ([0567885](https://github.com/AgriLogy/agrilogy-back/commit/056788584a33a2abb7fe6b3054426d34321580f2))
  * env example
  ([9896566](https://github.com/AgriLogy/agrilogy-back/commit/98965668f5d1cb66c501fba972eeb6b5051c875e))
  * update the data server
  ([184186b](https://github.com/AgriLogy/agrilogy-back/commit/184186b4b3fddf7a5cf91c57759761fbd798b96f))
  * user-zone mapping
  ([9c62202](https://github.com/AgriLogy/agrilogy-back/commit/9c62202067b8db86e80321a1a47e571d0eba7c6f))

### Features

* add data forward from js server
  ([d423d44](https://github.com/AgriLogy/agrilogy-back/commit/d423d44820f1c97df23dfbfeca05d2d255a2edfd))

### Features

- Add data forward from js server
  ([`d423d44`](https://github.com/AgriLogy/agri-api/commit/d423d44820f1c97df23dfbfeca05d2d255a2edfd))
