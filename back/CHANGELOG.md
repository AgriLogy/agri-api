# CHANGELOG


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
