#!/usr/bin/env bash
# One-shot local dev launcher: ensures .env exists, syncs deps, runs
# migrations, starts the Django dev server. Idempotent.
#
# Usage:
#   scripts/dev.sh            # runs on :8000
#   PORT=8001 scripts/dev.sh  # custom port
#
# Skip uv sync (e.g. if you already ran it) with SKIP_SYNC=1.
# Skip migrate with SKIP_MIGRATE=1.

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if ! command -v uv >/dev/null 2>&1; then
  echo "✘ uv is not installed."
  echo "  Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

# 1. Ensure back/.env exists with local-dev defaults.
if [[ ! -f back/.env ]]; then
  echo "→ Creating back/.env (local-dev defaults — edit if you need different values)"
  cat > back/.env <<'EOF'
SECRET_KEY=dev-only-not-secret-change-me
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
CORS_ALLOWED_ORIGINS=http://localhost:3000
CSRF_TRUSTED_ORIGINS=http://localhost:3000
USE_POSTGRES=False
POSTGRES_DB=
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_HOST=
POSTGRES_PORT=
CELERY_SCHEDULE_MODE=test
EOF
fi

# 2. Export everything in back/.env into the current shell so manage.py
#    sees the same values docker-compose's env_file would inject.
set -a
# shellcheck disable=SC1091
. back/.env
set +a

# 3. Sync the venv (creates back/.venv on first run, generates back/uv.lock).
if [[ "${SKIP_SYNC:-0}" != "1" ]]; then
  echo "→ uv sync"
  (cd back && uv sync)
fi

# 4. Migrate. SQLite file lands at back/db.sqlite3 when USE_POSTGRES=False.
if [[ "${SKIP_MIGRATE:-0}" != "1" ]]; then
  echo "→ python manage.py migrate"
  (cd back && uv run python manage.py migrate --noinput)
fi

# 5. Run the dev server. exec so Ctrl+C is forwarded cleanly.
port="${PORT:-8000}"
echo "→ Django dev server on http://0.0.0.0:${port}"
cd back
exec uv run python manage.py runserver "0.0.0.0:${port}"
