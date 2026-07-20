#!/usr/bin/env bash
#
# Single entrypoint shared by every backend container (web, worker, beat).
# The first argument selects the role; everything else is forwarded.
#
# Roles:
#   web      gunicorn when DJANGO_ENV=prod, else Django dev server, on :8000
#   fast     FastAPI sidecar (src/fastapp) via uvicorn on :8001
#   worker   Celery worker (Django app: celery -A agriapi)
#   beat     Celery beat with the DatabaseScheduler (Django app)
#   fast-worker  Native Celery worker (celery -A fastapp.celery_app) — F10 cutover of `worker`
#   fast-beat    Native Celery beat, static schedule — F10 cutover of `beat`
#   mqtt     MQTT ingest subscriber (fastapp.mqtt) — consumes ChirpStack +
#            generic sensor topics, writes via the fastapp.ingest handlers
#   migrate  Apply agri-db Alembic migrations, then exit (run by deploy)
#   shell    Drop into bash for debugging
#
# Designed to be idempotent — a container that crashes and restarts
# should converge to the same state without manual intervention.

set -euo pipefail

ROLE="${1:-web}"

# DJANGO_ENV picks the right settings module (settings/dev|prod|test.py).
export DJANGO_ENV="${DJANGO_ENV:-dev}"
shift || true

# SEED_DEV_USERS is read by seed_dev_users.py; we just echo the value
# the user picked (default: enabled for dev) in the boot log.
SEED_DEV_USERS_DEFAULT="${SEED_DEV_USERS:-true}"

log() { printf "\033[1;36m[entrypoint:%s]\033[0m %s\n" "$ROLE" "$*"; }

# --- Wait for Postgres ------------------------------------------------------
# Every role needs the DB up because Django imports ORM at module load.
wait_for_postgres() {
  case "${USE_POSTGRES:-false}" in
    [Tt][Rr][Uu][Ee]) ;;
    *)
      log "USE_POSTGRES != true — skipping DB wait."
      return 0
      ;;
  esac
  log "Waiting for postgres at ${POSTGRES_HOST:-agrydata}:${POSTGRES_PORT:-5432} ..."
  python - <<'PY'
import os, socket, time, sys

host = os.environ.get("POSTGRES_HOST", "agrydata")
port = int(os.environ.get("POSTGRES_PORT", "5432"))
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"  postgres reachable at {host}:{port}")
            sys.exit(0)
    except OSError:
        time.sleep(1)
print(f"  postgres not reachable at {host}:{port} after 60s", file=sys.stderr)
sys.exit(1)
PY
}

# --- Wait for Redis ---------------------------------------------------------
wait_for_redis() {
  log "Waiting for redis at ${CELERY_BROKER_URL:-redis://redis:6379/0} ..."
  python - <<'PY'
import os, socket, time, sys, urllib.parse

url = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
parsed = urllib.parse.urlparse(url)
host, port = parsed.hostname or "redis", parsed.port or 6379
deadline = time.time() + 30
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"  redis reachable at {host}:{port}")
            sys.exit(0)
    except OSError:
        time.sleep(1)
print(f"  redis not reachable at {host}:{port} after 30s", file=sys.stderr)
sys.exit(1)
PY
}

# --- Migrations -------------------------------------------------------------
# Schema is owned by the agri-db repo (Alembic). Django never runs `migrate`.
# In prod, the deploy pipeline applies Alembic migrations via the `migrate`
# role below (run before the app containers (re)start). For a fresh local
# stack: `make upgrade-dev` in agri-db BEFORE bringing this stack up.

# --- Roles ------------------------------------------------------------------
case "$ROLE" in
  web)
    wait_for_postgres
    log "Ensuring assistant tables exist (idempotent)"
    python scripts/ensure_assistant_tables.py || log "  (assistant table ensure skipped/failed; continuing)"
    log "Ensuring technician RBAC tables exist (idempotent)"
    python scripts/ensure_technician_tables.py || log "  (technician table ensure skipped/failed; continuing)"
    log "Ensuring business-admin tables exist (idempotent)"
    python scripts/ensure_admin_tables.py || log "  (admin table ensure skipped/failed; continuing)"
    python scripts/ensure_device_tables.py || log "  (device table ensure skipped/failed; continuing)"
    python scripts/ensure_irrigation_tables.py || log "  (irrigation table ensure skipped/failed; continuing)"
    python scripts/ensure_notification_zone_tables.py || log "  (notification-zone table ensure skipped/failed; continuing)"
    log "Ensuring sector table + zone.sector_id exist (idempotent)"
    python scripts/ensure_sector_tables.py || log "  (sector table ensure skipped/failed; continuing)"
    log "Ensuring device-health sensor tables exist (idempotent)"
    python scripts/ensure_sensor_health_tables.py || log "  (sensor-health table ensure skipped/failed; continuing)"
    log "Ensuring monitoring tables exist (idempotent)"
    python scripts/ensure_monitoring_tables.py || log "  (monitoring table ensure skipped/failed; continuing)"
    if [[ "$DJANGO_ENV" == "prod" ]]; then
      # Production: no dev seeders; serve via gunicorn with collected static
      # (WhiteNoise serves them since DEBUG=False).
      log "Collecting static files"
      python manage.py collectstatic --noinput
      log "Starting gunicorn on :8000 (workers=${GUNICORN_WORKERS:-3})"
      exec gunicorn agriapi.wsgi:application --bind 0.0.0.0:8000 \
        --workers "${GUNICORN_WORKERS:-3}" --timeout 60 \
        --access-logfile - --error-logfile -
    else
      log "Seeding dev users (idempotent; SEED_DEV_USERS=$SEED_DEV_USERS_DEFAULT)"
      python scripts/seed_dev_users.py || log "  (user seed skipped or failed; continuing)"
      log "Backfilling dev sensor data (idempotent; SEED_DEV_DATA=${SEED_DEV_DATA:-true})"
      python scripts/seed_dev_data.py || log "  (data seed skipped or failed; continuing)"
      log "Starting Django dev server on :8000"
      exec python manage.py runserver 0.0.0.0:8000
    fi
    ;;
  fast)
    # FastAPI strangler sidecar. Same prelude as `web` (the DB must be up —
    # fastapp's SQLAlchemy auth lookups hit it on first request); table
    # ensure/seed scripts stay web-only so the two roles never race them.
    wait_for_postgres
    log "Starting uvicorn (fastapp) on :8001 (workers=${UVICORN_WORKERS:-2})"
    # --no-access-log: RequestContextMiddleware emits the structured JSON access
    # line (event=http.access) with request-id; uvicorn's plain line would just
    # duplicate it. Application + error records still flow through our root JSON
    # handler (configured in fastapp.main at import).
    exec uvicorn fastapp.main:app --host 0.0.0.0 --port 8001 \
      --workers "${UVICORN_WORKERS:-2}" --no-access-log
    ;;
  worker)
    wait_for_postgres
    wait_for_redis
    log "Starting Celery worker"
    # -Q agriapi: consume only our own queue (CELERY_TASK_ROUTES sends every
    # agriapi.* task here). Keeps the legacy agriBack.* tasks — which share this
    # broker on the default queue — off this worker, and stops our tasks being
    # silently dropped onto the legacy worker.
    exec celery -A agriapi worker --loglevel=info --concurrency=1 -Q agriapi
    ;;
  beat)
    wait_for_postgres
    wait_for_redis
    log "Starting Celery beat"
    exec celery -A agriapi beat --loglevel=info \
      --scheduler django_celery_beat.schedulers:DatabaseScheduler
    ;;
  fast-worker)
    # F10: native (Django-free) Celery worker running the ported task bodies
    # (fastapp.celery_app registers them under the same agriapi.tasks.* names on
    # the same `agriapi` queue). This REPLACES the `worker` role at cutover — the
    # two must never run together or every task double-executes off the shared
    # queue. Rollback = switch the compose command back to `worker`.
    wait_for_postgres
    wait_for_redis
    log "Starting native Celery worker (fastapp.celery_app)"
    exec celery -A fastapp.celery_app worker --loglevel=info --concurrency=1 -Q agriapi
    ;;
  fast-beat)
    # F10: native Celery beat with the STATIC schedule in fastapp.celery_app
    # (PersistentScheduler). REPLACES the `beat` (DatabaseScheduler) role at
    # cutover. Reconcile the static schedule against the live PeriodicTask rows
    # BEFORE flipping. Rollback = switch the compose command back to `beat`.
    wait_for_postgres
    wait_for_redis
    log "Starting native Celery beat (fastapp.celery_app, static schedule)"
    exec celery -A fastapp.celery_app beat --loglevel=info \
      --schedule "${CELERYBEAT_SCHEDULE_FILE:-/tmp/fastapp-celerybeat-schedule}"
    ;;
  mqtt)
    # MQTT ingest subscriber: a persistent paho loop_forever() client that
    # consumes ChirpStack LoRaWAN uplinks + the generic sensor topic tree and
    # writes through the SAME fastapp.ingest handlers the HTTP /ingest/* webhooks
    # use (additive — HTTP keeps working). Needs Postgres (readings + alert grace
    # claim) and Redis (alert tasks enqueue via fastapp.celery.send_task).
    # Run EXACTLY ONE instance — every subscriber gets every message.
    wait_for_postgres
    wait_for_redis
    log "Starting MQTT ingest subscriber (fastapp.mqtt)"
    exec python -m fastapp.mqtt
    ;;
  migrate)
    # Apply the bundled agri-db Alembic migrations to the live database, then
    # exit. Run by the deploy pipeline BEFORE the app containers are (re)started,
    # so new code never boots against an un-migrated schema.
    #
    # Gated OFF by default: until the live DB is reconciled with Alembic (see
    # below), this is a no-op so merging the deploy wiring can't change prod.
    # Flip RUN_DB_MIGRATIONS=true in the env once the cutover is done.
    if [[ "${RUN_DB_MIGRATIONS:-false}" != "true" ]]; then
      log "RUN_DB_MIGRATIONS != true — skipping Alembic migrations (no-op)."
      exit 0
    fi
    wait_for_postgres
    # Django builds DATABASES and mirrors it into AGRI_DB_URL
    # (postgresql+psycopg://… — the very URL agri-core/SQLAlchemy use). Alembic's
    # env.py reads DATABASE_URL, so resolve one and hand it across.
    log "Resolving the live database URL from Django settings"
    DB_URL="$(DJANGO_SETTINGS_MODULE=agriapi.settings python -c 'import django; django.setup(); import os; print(os.environ.get("AGRI_DB_URL", ""))')"
    if [[ -z "$DB_URL" ]]; then
      log "Could not resolve AGRI_DB_URL (non-Postgres or unset) — aborting."
      exit 1
    fi
    export DATABASE_URL="$DB_URL"
    # Has this DB ever been Alembic-managed? A legacy DB bootstrapped by the
    # ensure_*-scripts already has every table but no alembic_version row, so
    # replaying from base would fail ("relation already exists"). That one-time
    # reconciliation must be an EXPLICIT stamp to the revision matching the live
    # schema — never a guess. Set ALEMBIC_STAMP_REV (e.g. "head" once the pinned
    # agri-db == the live schema) for that first run, then unset it.
    if python -c 'import os,sqlalchemy as sa,sys; sys.exit(0 if sa.inspect(sa.create_engine(os.environ["DATABASE_URL"])).has_table("alembic_version") else 1)'; then
      log "alembic_version present → upgrade head"
      exec agri-migrate upgrade head
    elif [[ -n "${ALEMBIC_STAMP_REV:-}" ]]; then
      log "alembic_version absent → one-time stamp to '$ALEMBIC_STAMP_REV' (legacy DB reconciliation)"
      exec agri-migrate stamp "$ALEMBIC_STAMP_REV"
    else
      log "alembic_version absent and ALEMBIC_STAMP_REV unset — refusing to replay from base on a populated DB."
      log "Set ALEMBIC_STAMP_REV to the revision matching the live schema (once), then redeploy."
      exit 1
    fi
    ;;
  shell)
    exec bash "$@"
    ;;
  *)
    echo "Unknown role: $ROLE" >&2
    echo "Use one of: web | fast | worker | beat | fast-worker | fast-beat | mqtt | migrate | shell" >&2
    exit 2
    ;;
esac
