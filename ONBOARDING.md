# ONBOARDING — agri-api

Orientation for a new contributor (or a fresh agent session). Deeper docs:
`CLAUDE.md`, `.claude/QUICK_START.md`, `docs/INDEX.md`.

## What this repo is

The Agrilogy HTTP API service. Django 4.2 lives in `back/` (src/ layout:
`back/src/agriapi` project + `back/src/apps/*` domain apps), with Celery
worker/beat riding the same image. **New endpoints use django-ninja**
(FastAPI-style routers + pydantic schemas) while legacy DRF views migrate
incrementally.

**Strangler migration in progress:** the long-term target is FastAPI. A
sidecar app lives at `back/src/fastapp/` — same pyproject/image/env as
Django, served by uvicorn on :8001 (`docker-entrypoint.sh fast`, compose
service `agri-api-fast`). nginx on the droplet cuts path prefixes over from
Django (:8000) one at a time; the repo-tracked nginx config and the cutover
recipe live in **`deploy/nginx/back.conf`** (applying it on the droplet is
still manual: `nginx -t && systemctl reload nginx`).

## Dependency chain

```
agri-api  →  agri-core (handlers + business logic, SQLAlchemy DB access)
                 →  agri-db (Postgres schema-of-record, Alembic, PRIVATE)
```

- Both links are **tag-pinned git dependencies** in `back/pyproject.toml`
  (e.g. `agri-core @ git+https://github.com/AgriLogy/agri-core.git@<tag>`);
  bumping agri-core is a normal dep-bump PR + `uv lock`.
- agri-db is **private**, pulled transitively through agri-core — so
  `uv sync`, CI, and the Docker build all need the `AGRI_DB_RO_TOKEN`
  read-only PAT as a git credential (CI passes the org secret; the Docker
  build takes it as a BuildKit secret; see `back/Dockerfile`).
- Django and SQLAlchemy share one database: Django settings mirror their
  `DATABASES` into `AGRI_DB_URL` (`export_agri_db_url`) so agri-core's
  handlers connect to the same Postgres.

## Bootstrap order (local dev)

1. **Schema first** — the schema is owned by the sibling `agri-db` repo
   (Alembic). Never `manage.py migrate`/`makemigrations` here:
   `cd ../agri-db && make upgrade-dev`
2. **Env** — fill `back/.env` (start from `back/env-example`): Postgres
   creds, SECRET_KEY, etc. Both Django and the fastapp sidecar read this
   same file/env names.
3. **Stack** — `make up` (redis + django web + celery worker/beat + mailpit
   + agri-bridge + agri-api-fast, all from `docker-compose.yml`).

Tests/lint from `back/`: `uv run pytest`, `uv run ruff check src/`,
`uv run ruff format src/`. The dual-ORM suites need Postgres (they skip on
sqlite); CI runs everything against a Postgres 16 service.

## CI — thin callers into shared-workflows

CI logic is centralized in **`AgriLogy/shared-workflows`** (pinned `@v1`);
this repo's `.github/workflows/` files are thin callers that own only
triggers + repo-specific inputs:

| Caller | Shared workflow | Notes |
| --- | --- | --- |
| `primary.yml` | `python-lint.yml` + `python-test.yml` | lint → test; Postgres 16 service, Django check, coverage floor 85, full `src/` pytest run |
| `release.yml` | `release.yml` | `[skip ci]` guard + concurrency stay caller-side |
| `lint-pr-title.yml` | `lint-pr-title.yml` | Conventional-Commit PR titles (squash-merge title = release commit) |
| `auto-assign.yml` | `auto-assign.yml` | assigns new issues/PRs to mks-zakaria |
| `deploy-back.yml` | `deploy-droplet.yml` | see Deploy below |

## Release flow

`python-semantic-release` on push to main: conventional commits since the
last tag decide the bump (`feat` → minor, `fix`/`perf` → patch), version is
rewritten in `back/pyproject.toml`, tag format **`v{version}`** (service
repo; the libraries agri-db/agri-core use bare tags). rc prereleases come
from `feat|fix|perf/` branches via `workflow_dispatch`. Release commits are
authored by MKS~ZAK and carry `[skip ci]`.

## Deploy

Push to `main` → `deploy-back.yml` SSHes into the droplet
(`/root/agri-api`), hard-resets to `origin/main`, **rebuilds** the image
(the venv is baked in — restart-only deploys break on dependency changes),
runs the entrypoint `migrate` role (no-op until `RUN_DB_MIGRATIONS=true`;
see `docs/MIGRATIONS_PROD_CUTOVER.md`), then recreates ONLY
`agri-api-web agri-api-worker agri-api-beat` with `--no-deps` and prunes
dangling images. Health check: `curl -s -o /dev/null -w '%{http_code}'
https://back.agrogo-datafarm.com/admin/login/` → 200.

## fastapp cutover mechanics (strangler)

When a route family is ready to move to the sidecar: implement it in
`back/src/fastapp/` (auth parity is already in place — Django-minted
simplejwt tokens verify against the shared user table via
`fastapp/auth.py`), deploy, then add a `location /<prefix>/ { proxy_pass
http://127.0.0.1:8001; ... }` block in `deploy/nginx/back.conf` and mirror
it on the droplet. Rollback = delete the block. Full recipe in that file's
header.
