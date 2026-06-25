#!/usr/bin/env bash
#
# Single entrypoint shared by every backend container (web, worker, beat).
# The first argument selects the role; everything else is forwarded.
#
# Roles:
#   web      gunicorn when DJANGO_ENV=prod, else Django dev server, on :8000
#   worker   Celery worker
#   beat     Celery beat with the DatabaseScheduler
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
# Schema is owned by the agri-db repo (Alembic). Django no longer runs
# `migrate` on container boot — see ../agri-db.
# To bootstrap a fresh Supabase project: `make upgrade-dev` in agri-db
# BEFORE bringing this stack up.

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
  shell)
    exec bash "$@"
    ;;
  *)
    echo "Unknown role: $ROLE" >&2
    echo "Use one of: web | worker | beat | shell" >&2
    exit 2
    ;;
esac
