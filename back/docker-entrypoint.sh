#!/usr/bin/env bash
#
# Single entrypoint shared by every backend container (web, worker, beat).
# The first argument selects the role; everything else is forwarded.
#
# Roles:
#   web      Django dev server on :8000 (after running migrations)
#   worker   Celery worker
#   beat     Celery beat with the DatabaseScheduler
#   shell    Drop into bash for debugging
#
# Designed to be idempotent — a container that crashes and restarts
# should converge to the same state without manual intervention.

set -euo pipefail

ROLE="${1:-web}"
shift || true

# SEED_DEV_USERS is read by seed_dev_users.py; we just echo the value
# the user picked (default: enabled for dev) in the boot log.
SEED_DEV_USERS_DEFAULT="${SEED_DEV_USERS:-true}"

log() { printf "\033[1;36m[entrypoint:%s]\033[0m %s\n" "$ROLE" "$*"; }

# --- Wait for Postgres ------------------------------------------------------
# Every role needs the DB up because Django imports ORM at module load.
wait_for_postgres() {
  if [[ "${USE_POSTGRES:-False}" != "True" ]]; then
    log "USE_POSTGRES != True — skipping DB wait."
    return 0
  fi
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
# Schema is owned by the agrilogy-db repo (Alembic). Django no longer runs
# `migrate` on container boot — see /Users/mks/agrilogy/agrilogy-db.
# To bootstrap a fresh Supabase project: `make upgrade-dev` in agrilogy-db
# BEFORE bringing this stack up.

# --- Roles ------------------------------------------------------------------
case "$ROLE" in
  web)
    wait_for_postgres
    log "Seeding dev users (idempotent; SEED_DEV_USERS=$SEED_DEV_USERS_DEFAULT)"
    python seed_dev_users.py || log "  (user seed skipped or failed; continuing)"
    log "Backfilling dev sensor data (idempotent; SEED_DEV_DATA=${SEED_DEV_DATA:-true})"
    python seed_dev_data.py || log "  (data seed skipped or failed; continuing)"
    log "Starting Django dev server on :8000"
    exec python manage.py runserver 0.0.0.0:8000
    ;;
  worker)
    wait_for_postgres
    wait_for_redis
    log "Starting Celery worker"
    exec celery -A agriBack worker --loglevel=info --concurrency=1
    ;;
  beat)
    wait_for_postgres
    wait_for_redis
    log "Starting Celery beat"
    exec celery -A agriBack beat --loglevel=info \
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
