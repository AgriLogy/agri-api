# Prod migration cutover — turning on auto-apply

The deploy pipeline can apply agri-db Alembic migrations to the live
`agrydata` database automatically, just before the app containers restart
(`docker-entrypoint.sh migrate`, wired into `.github/workflows/deploy-back.yml`).

It ships **OFF** (`RUN_DB_MIGRATIONS` unset → the step is a logged no-op) because
the live DB was bootstrapped by the idempotent `ensure_*` scripts, not by
Alembic — so its `alembic_version` table does not exist yet, and the bundled
agri-db version lags the live schema. Enabling it naively would either replay
the baseline onto populated tables (fails) or stamp the wrong revision (hides
drift). Do the one-time reconciliation below first.

## Why a cutover is needed

- **Pin gap.** agri-api pins `agri-core` (which pins agri-db). The container
  currently bundles **agri-db 0.8.0**, but the live schema is at **0.11.x**.
  The migrate step runs the *bundled* agri-db's migrations, so the bundle must
  be `>=` the live schema or `upgrade head` will try to re-apply already-present
  migrations and fail.
- **Unstamped DB.** No `alembic_version` row exists on `agrydata` yet.

## One-time steps

1. **Close the pin gap (release chain).** Bump agri-db → agri-core → agri-api so
   the container bundles the agri-db version whose head matches the live schema:
   - agri-core: bump its agri-db pin to `0.11.1` (or later), release a new
     agri-core (e.g. `0.19.0`).
   - agri-api: bump `agri-core` to that release in `back/pyproject.toml` +
     `uv lock`. Let CI (full pytest+cov) validate compatibility.

2. **Verify the live schema actually matches that head.** Because the DB was
   hand-built, confirm the columns/tables the target revision expects are all
   present before stamping (spot-check the most recent migrations:
   `notify_*`, `grace_override_seconds`, `elevation_m`, notification-zone
   tables, `water_level_status`).

3. **First enable = stamp, not upgrade.** On the droplet `/root/agri-api/back/.env`:
   ```
   RUN_DB_MIGRATIONS=true
   ALEMBIC_STAMP_REV=head     # only needed for the very first deploy
   ```
   Deploy. The migrate step sees no `alembic_version`, stamps it to the bundled
   head (== the live schema after step 1), and exits. Verify:
   `docker compose run --rm --no-deps agri-api-web agri-migrate current`.

4. **Remove the one-shot.** Delete `ALEMBIC_STAMP_REV` from `.env` (keep
   `RUN_DB_MIGRATIONS=true`). From now on every deploy runs `upgrade head`,
   applying only genuinely-new migrations. A failed migration aborts the deploy
   (`script_stop: true`) before any new code starts — by design.

## Steady state (after cutover)

Add a migration in agri-db → `make migrate-test` (empty-DB gate) → push (CI
gate) → merge/release → bump agri-core + agri-api pins → merge agri-api → the
deploy applies it to `agrydata` automatically, then starts the new code.
