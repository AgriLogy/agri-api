# Contributing to `agri-api`

Everything below is derived from files in this repo. If a command isn't here, it probably
doesn't exist.

---

## 1. What this repo is

The Agrilogy **HTTP API service**: Django 4.2 + DRF + django-ninja on `:8000`, plus the
FastAPI strangler sidecar (`back/src/fastapp`) on `:8001`, plus Celery worker/beat and an
MQTT ingest subscriber — all from the same image, specialised by `docker-entrypoint.sh <role>`.

Dependency chain is **one-way**:

```
agri-api  →  agri-core (handlers + business logic, SQLAlchemy DB access)
                 →  agri-db (Postgres schema-of-record, Alembic, PRIVATE)
```

| Rule | Why |
| --- | --- |
| Web frameworks (Django/DRF/ninja/FastAPI) live **only** in agri-api | agri-core is framework-agnostic and reused by other services |
| Business logic + DB access belongs in **agri-core** | api is a thin transport shell |
| Schema is owned by **agri-db** (Alembic) | Django and SQLAlchemy share one database |
| **Never** run `manage.py migrate` / `makemigrations` against a real DB here | Django migrate is skipped on container boot on purpose (`back/docker-entrypoint.sh`); schema drift is the failure mode |

Both `agri-core` and (transitively) `agri-db` are **tag-pinned git dependencies** in
`back/pyproject.toml`. Bumping agri-core is a normal dep-bump PR + `uv lock`.

---

## 2. Prerequisites

| Tool | Version | Source |
| --- | --- | --- |
| Python | `>=3.12` (image uses `python:3.12.8-slim`) | `back/pyproject.toml`, `back/Dockerfile` |
| uv | `0.11.6` in CI and the Docker image | `back/Dockerfile`, `.github/workflows/primary.yml` |
| Docker + Compose v2 | any recent | `docker-compose.yml` (uses `docker compose`) |
| Postgres | 16 in CI; Supabase pooler or the droplet's `agrydata` in real envs | `.github/workflows/primary.yml` |
| mosquitto | only for the MQTT broker/e2e tests | `primary.yml` `mqtt-e2e` job |
| `AGRI_DB_RO_TOKEN` | read-only GitHub PAT | needed by `uv sync`, CI, and the Docker build to fetch the **private** agri-db |

Node is **not** required for this repo (the `agri-bridge` compose service builds from the
sibling `../agri-bridge` checkout).

---

## 3. First-time setup

```bash
# 0. uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 1. Private agri-db needs a PAT for uv to resolve it (agri-core pulls it transitively)
export AGRI_DB_RO_TOKEN=<read-only PAT>
git config --global \
  url."https://x-access-token:${AGRI_DB_RO_TOKEN}@github.com/".insteadOf \
  "https://github.com/"

# 2. Deps + git hooks (commit-msg + pre-push)
make bootstrap        # = cd back && uv sync --frozen  +  ./scripts/install-hooks.sh

# 3. SCHEMA FIRST — always, before bringing this stack up
cd ../agri-db && make upgrade-dev && make current-dev && cd ../agri-api

# 4. Env
cp back/env-example back/.env    # fill POSTGRES_* (Supabase Session pooler), SECRET_KEY, USE_POSTGRES=True

# 5. Bring the stack up
make up
```

Two ways to run locally:

```bash
make dev     # SQLite, no Docker: scripts/dev.sh writes back/.env defaults, uv sync, runserver :8000
             # overrides: PORT=8001 make dev | SKIP_SYNC=1 make dev | SKIP_MIGRATE=1 make dev
make up      # full compose stack (see table below)
```

> `make dev` *does* run `manage.py migrate` — that is fine because it targets the throwaway
> local `back/db.sqlite3`. Never point it at Postgres.

Local URLs: Django `:8000` (`/admin/`, `/swagger/`, `/redoc/`), fastapp `:8001` (`/healthz`),
Mailpit UI `:8025` (SMTP `:1025`), agri-bridge `:9090`, mosquitto `:1883`, redis `:6379`.

Compose services (`docker-compose.yml`): `agri-api-web` (Django), `agri-api-fast` (uvicorn/fastapp),
`agri-api-worker` (`fast-worker` role), `agri-api-beat` (`fast-beat` role), `agri-api-mqtt`,
`redis`, `mailpit`, `mosquitto`, `agri-bridge`.

---

## 4. Day-to-day loop

```bash
make up / make down / make re / make stat / make build   # docker compose
make install                                             # uv sync --frozen (in back/)
make lint                                                # ruff check
make format                                              # ruff format (writes)
make format-check                                        # ruff format --check
make check                                               # lint + format-check (pre-push gate)
make test                                                # DJANGO_ENV=test manage.py test --noinput
```

Direct pytest (what CI actually runs):

```bash
cd back
uv run pytest src/ -v
uv run pytest src/fastapp/tests/test_sectors.py -v
uv run python manage.py check
```

Dual-ORM / handler tests need Postgres and **skip** on SQLite (`back/conftest.py` binds
`AGRI_DB_URL` to Django's test DB). MQTT broker/e2e tests skip unless a real `mosquitto`
binary is present — set `MQTT_REQUIRE_BROKER=1` to make them fail instead of skip.

There is **no typecheck step** in this repo — no mypy/pyright config exists. Ruff (`select = ["F"]`,
line-length 88, `target-version = py312`) is the only static gate.

Optional: `uvx pre-commit install` — `.pre-commit-config.yaml` runs the same ruff hooks plus a
pre-push `pytest src/apps/users/ src/analytics/` and a semantic-release config validation.

---

## 5. Repo layout

| Path | What belongs there |
| --- | --- |
| `back/src/agriapi/` | Django project: `settings/`, `urls.py`, `celery.py`, `tasks.py`, exception handler, middleware, email/WhatsApp backends |
| `back/src/apps/` | Vertical-slice Django apps: `users`, `sensors`, `irrigation`, `alerts`, `bivocom`, `lorawan`, `assistant`, `feedback` — **new Django code goes here** |
| `back/src/analytics/` | Legacy god-app. Do not add to it (see `.claude/COMMON_MISTAKES.md` #7) |
| `back/src/fastapp/` | FastAPI strangler target: `main.py`, `routers/`, `auth.py`, `ingest.py`, `mqtt.py`, `celery_app.py`, `tasks_*.py`, `tests/` — **new API surface goes here** |
| `back/scripts/` | Idempotent `ensure_*_tables.py` bootstrap scripts + dev seeders, run by the `web` entrypoint role |
| `back/Dockerfile`, `back/docker-entrypoint.sh` | One image, role dispatch: `web \| fast \| worker \| beat \| fast-worker \| fast-beat \| mqtt \| migrate \| shell` |
| `deploy/nginx/back.conf` | **Source of truth** for the droplet's nginx strangler routing |
| `deploy/mosquitto/`, `deploy/observability/` | Dev broker config; Loki/Promtail/Grafana stack |
| `docs/` | `INDEX.md`, `MIGRATIONS_PROD_CUTOVER.md`, `SSO_CORS_ORIGINS.md`, `flows/*` |
| `scripts/` | `dev.sh`, `install-hooks.sh`, `smoke.sh`, `project-sync.sh` |
| `.githooks/` | `commit-msg` (Conventional Commits) + `pre-push` (blocks pushes to `main`, runs ruff) |
| `shared_data/` | Bind-mounted runtime data — never source |

---

## 6. Adding a change end-to-end (worked example: a new `/sectors` style endpoint)

1. **Schema (only if new columns/tables):** open a PR in `../agri-db` adding the Alembic
   revision + model, release a tag; bump agri-core's agri-db pin and release agri-core.
2. **Logic:** put queries/business rules in **agri-core** (`agri.core.*`). Release a tag.
3. **Pin here:** update `agri-core @ git+https://github.com/AgriLogy/agri-core.git@<tag>` in
   `back/pyproject.toml`, then `cd back && uv lock` (commit `uv.lock`).
4. **Router:** add `back/src/fastapp/routers/<name>.py` — `router = APIRouter(tags=[...])`,
   pydantic schemas in-file, auth via `Depends(get_current_user)` from `fastapp/auth.py`,
   responses via `DjangoStyleJSONResponse` (keeps byte-parity with the Django surface).
   Register it in `back/src/fastapp/main.py` with `app.include_router(...)` and a comment
   naming the F-phase/prefix, exactly like the existing lines.
5. **Tests:** add `back/src/fastapp/tests/test_<name>.py`. When you are *moving* an existing
   Django route, write a `*_parity.py` test asserting identical status + body against the
   Django view — that's the established pattern (`test_weather_parity.py`, `test_users_parity.py`).
6. **Route it:** add a `location /<prefix>/ { proxy_pass http://127.0.0.1:8001; ... }` block
   **above** the catch-all `location /` in `deploy/nginx/back.conf` (most specific first),
   with a comment stating phase + date. Rollback = delete the block.
7. **Verify:** `cd back && uv run ruff check . && uv run ruff format . && uv run pytest src/ -v`,
   then `BASE_URL=http://localhost:8000 ./scripts/smoke.sh`.
8. **Docs:** update `docs/flows/*` if you changed a flow; `docs/INDEX.md` if you added a doc.

Pure-Django change? Same shape, but the view lives under `back/src/apps/<slice>/` — never
`back/src/analytics/`.

---

## 7. Branches, commits, PRs

- Branch off **`main`**. Direct pushes to `main` are blocked locally (`.githooks/pre-push`,
  protected list: `main master back front`) and by branch protection.
- **`main` = staging**: merging triggers `release.yml` (python-semantic-release) and
  `deploy-back.yml` (SSH deploy to the droplet).
- **`alpha` = production**: promoted by a *manual* merge from `main`. Never auto-synced.
  ⚠️ Not documented in-repo: this repo currently has no `alpha` branch and no workflow keyed
  to it — `deploy-back.yml` fires on `push: branches: [main]` only. Treat the alpha promotion
  as the org-wide release convention until a workflow lands here.
- **Conventional Commits, enforced twice**: `.githooks/commit-msg` locally and the
  `Lint PR title` workflow on GitHub. Format `<type>(<scope>)?: <subject>`; types
  `feat|fix|perf|refactor|docs|style|test|build|ci|chore|revert`; subject starts with a letter,
  ≤100 chars, no trailing period. Squash-merge uses the **PR title** as the release commit —
  so the title is what semantic-release classifies.
- **Every PR references exactly one dedicated, scope-matched issue** with `Closes #N` in the
  body. One issue, one PR, matching scope — no umbrella issues.
- **Assignment**: both issue and PR are assigned to the author (`mks-zakaria`).
  `auto-assign.yml` does this automatically on open.
- **Zero AI/assistant attribution** anywhere: no `Co-Authored-By: Claude`, no "generated with"
  footers, no assistant names in branch names, commit bodies, PR titles/descriptions, or issues.
  The sole author is `mks-zakaria`.
- **Commit from your local machine only** — never `git commit` over SSH on the droplet.
- Never hand-edit `back/pyproject.toml:version` or `CHANGELOG.md`; semantic-release owns them.
- Emergency bypass of the pre-push gate: `PRE_PUSH_SKIP=1 git push ...` (use sparingly).

---

## 8. CI

Workflows are **thin callers into `AgriLogy/shared-workflows@v1`**; this repo owns only the
triggers and inputs.

| Workflow | Trigger | What runs |
| --- | --- | --- |
| `primary.yml` → `lint` | PR + push to `main` | `uv sync --frozen` + `ruff check` + `ruff format --check`, `working_directory: back` |
| `primary.yml` → `test` (needs lint) | PR + push to `main` | Postgres 16 service, `manage.py check`, `pytest src/ -v`, coverage floor **85** |
| `primary.yml` → `mqtt-e2e` (needs lint) | PR + push to `main` | installs real `mosquitto` + Postgres 16, runs `test_mqtt_broker.py` + `test_mqtt_e2e.py` with `MQTT_REQUIRE_BROKER=1` |
| `lint-pr-title.yml` | PR opened/edited/synchronize | Conventional-Commit PR title |
| `auto-assign.yml` | issue/PR opened | assigns `mks-zakaria` |
| `release.yml` | push to `main` (skipped on `[skip ci]`) | python-semantic-release |
| `deploy-back.yml` | push to `main`, `workflow_dispatch` | droplet deploy |

Reproduce the PR gate locally (Postgres reachable on `localhost:5432`):

```bash
cd back
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
DJANGO_SETTINGS_MODULE=agriapi.settings SECRET_KEY=ci-placeholder-secret-key DEBUG=True \
ALLOWED_HOSTS='*' USE_POSTGRES=True POSTGRES_HOST=localhost POSTGRES_PORT=5432 \
POSTGRES_USER=postgres POSTGRES_PASSWORD=postgres POSTGRES_DB=postgres \
EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend CELERY_TASK_ALWAYS_EAGER=True \
  uv run python manage.py check && \
  uv run pytest src/ -v --cov --cov-fail-under=85
```

---

## 9. Release & deploy

**Release** (`release.yml`, push to `main`): python-semantic-release parses commits since the
last tag — `feat` → minor, `fix`/`perf` → patch — rewrites `version` in `back/pyproject.toml`
(`version_toml`), updates `CHANGELOG.md`, tags **`v{version}`**, and pushes a
`chore(release): v{version} ... [skip ci]` commit authored by `MKS~ZAK`. rc prereleases come
from `feat|fix|perf/*` branches via `workflow_dispatch` with `release_type=rc`.

**Deploy** (`deploy-back.yml` → shared `deploy-droplet.yml`), on push to `main`:

1. SSH to the droplet, `git reset --hard origin/main` in `/root/agri-api`
2. Rebuild the image — build only `agri-api-web` (worker/beat/fast/mqtt share it), with
   `AGRI_DB_RO_TOKEN` forwarded as a BuildKit secret
3. Run the `migrate` role: `bash /code/docker-entrypoint.sh migrate`
4. `up -d --no-deps` for `agri-api-web agri-api-worker agri-api-beat agri-api-fast`, then prune

Health check: `curl -s -o /dev/null -w '%{http_code}' https://back.agrogo-datafarm.com/admin/login/` → `200`.

### Migration ordering rule (non-negotiable)

Apply the agri-db migrations **before** deploying an agri-api that expects them:

```bash
cd ../agri-db && make upgrade-dev     # or upgrade-prod
cd ../agri-api                        # then merge/deploy
```

The in-pipeline `migrate` step is **OFF by default** (`RUN_DB_MIGRATIONS` unset → logged
no-op) because the live `agrydata` DB was bootstrapped by the `ensure_*` scripts and has no
`alembic_version`. The one-time reconciliation (close the agri-db→agri-core→agri-api pin gap,
verify the live schema, first run = `ALEMBIC_STAMP_REV=head` stamp, then remove the one-shot)
is written up in **`docs/MIGRATIONS_PROD_CUTOVER.md`**. Follow it; don't improvise a stamp.

---

## 10. Gotchas

- **Strangler state.** Django (`:8000`) and fastapp (`:8001`) both run. Which one serves a path
  in prod is decided by nginx, **not** by code. `deploy/nginx/back.conf` is the tracked source
  of truth, and applying it is still **manual**: edit here, mirror on the droplet, then
  `nginx -t && systemctl reload nginx`. `deploy-back.yml` does not sync it.
- **Location-block order matters.** Cutover blocks go *above* the catch-all `location /`, most
  specific first. Rollback of a cutover = delete the block (Django still serves the route).
- **Baked venv.** The image bakes the venv while `./back` is bind-mounted as code. A pull
  without a rebuild produces 500s on any dependency change — deploys must rebuild.
- **`agri-api-mqtt` is not in the deploy's recreate list.** After a deploy that changes MQTT
  code, run `docker compose up -d --no-deps --force-recreate agri-api-mqtt` on the droplet.
- **Never scale `agri-api-mqtt` past 1 replica** — every subscriber gets every message, so N
  replicas = N× writes and N× alert emails.
- **Web healthcheck `start_period: 180s` is load-bearing.** The `web` role chains the
  `ensure_*_tables` scripts before serving; a short grace made `compose up --wait` mark it
  unhealthy and left worker/beat stranded in `created` (Celery silently down). If that happens,
  `docker start` them. Worker/beat/mqtt intentionally depend on `service_started`, not
  `service_healthy`.
- **The compose network is external** (`agrilogy-back_agro`). A non-external network isolates
  the containers from `agrydata`/`redis` → "postgres not reachable at agrydata:5432" → 502.
- **`USE_POSTGRES` compare is case-sensitive** in `back/docker-entrypoint.sh` — `true` (lowercase)
  bypasses `wait_for_postgres`. Cold-start race risk; use `True`.
- **`AGRI_DB_RO_TOKEN` missing** = `uv sync` / Docker build fails to resolve the private agri-db.
  Symptom is a git-auth error on `agri-db`, not a helpful message.
- **Don't add to `back/src/analytics/`** (legacy god-app) and don't commit runtime artifacts
  (`db.sqlite3`, `celerybeat-schedule`, `__pycache__`, `shared_data/` output).
- **Dual-ORM tests silently skip** without Postgres, and MQTT e2e tests silently skip without a
  `mosquitto` binary. A green local run is not a green CI run.
- **Trailing slashes.** Django (`APPEND_SLASH`) and the fastapp routes are not uniformly
  slash-tolerant, and clients have been bitten by it; the exact per-route behaviour is
  **not documented in-repo** — mirror the existing path spelling in `deploy/nginx/back.conf`
  and the parity tests rather than guessing.
- **`.claude/` is gitignored** — useful local context (`COMMON_MISTAKES.md`, `QUICK_START.md`,
  `ARCHITECTURE_MAP.md`, `CONTINUE.md`), never a place to put things others need.
