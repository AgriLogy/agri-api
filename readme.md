# Agrilogy Backend – Development & Contribution Guide

This repository contains the **Django backend** for the Agrilogy platform.

The project is designed to be **automated, consistent, and safe for collaboration**, especially for junior developers.
Most rules here exist to **prevent mistakes before they reach production**.

Please read this document carefully before contributing.

---

## 🧠 Project Philosophy

We optimize for:

- Consistency over personal preferences
- Automation over manual steps
- Preventing bugs early (before deployment)
- Easy onboarding for new and junior developers
- Production-grade engineering standards

Formatting, linting, and deployment are **not optional**.

---

## 🚀 Local setup

### 1. Install [uv](https://docs.astral.sh/uv/) (Python package manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Bootstrap the repo

```bash
make bootstrap
```

That runs:

- `cd back && uv sync` — creates `back/.venv` with prod + dev deps from `back/pyproject.toml` and `back/uv.lock`.
- `./scripts/install-hooks.sh` — wires `.githooks/` into git so `commit-msg` and `pre-push` fire locally.

### 3. Run the stack

#### Quick local dev (SQLite, no Docker)

```bash
make dev
```

That runs `scripts/dev.sh`: creates `back/.env` with safe defaults if it's missing, exports the vars, `uv sync`, `manage.py migrate`, then starts the dev server on `:8000`. SQLite file lives at `back/db.sqlite3`.

Useful overrides: `PORT=8001 make dev`, `SKIP_SYNC=1 make dev`, `SKIP_MIGRATE=1 make dev`.

#### Full stack (Supabase Postgres + local Redis + Mailpit)

Postgres is hosted on **Supabase** (see [agri-db](../agri-db)). The
local Docker stack no longer ships a Postgres container.

```bash
# 1) Make sure the Supabase project's schema is up to date
( cd ../agri-db && make upgrade-dev )

# 2) Fill in back/.env with the Session pooler URI from Supabase Studio
cp back/env-example back/.env   # then paste POSTGRES_* values, set USE_POSTGRES=True

# 3) Bring up the stack
make up                         # docker compose: redis + mailpit + django + celery worker + beat
```

Django on `:8000`, **Mailpit web UI on `:8025`** (SMTP `:1025`). Browse
Postgres via Supabase Studio.

> **Schema changes go through [agri-db](../agri-db), not Django.**
> Django no longer runs `migrate` on container boot. Adding a Django model
> field requires a parallel Alembic migration in agri-db. This is a
> deliberate split so the (planned) FastAPI rewrite inherits the same
> migration history.

#### Verifying outgoing email locally

`back/.env` defaults to `EMAIL_BACKEND=` unset → console backend in DEBUG. To prove SMTP wiring against Mailpit:

```bash
docker compose up -d mailpit
# in back/.env:
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=localhost   # or `mailpit` if Django is also in compose
EMAIL_PORT=1025
EMAIL_USE_TLS=False
EMAIL_USE_SSL=False
make dev
```

Trigger any of:
- `GET /auth/send-notification/` (auth required) — sends to `request.user.email`
- `POST /api/zone-notification-outbound/` with `channels.email: true` — confirmation email when a zone config is saved
- Celery beat fires `agriapi.tasks.send_periodic_notifications` on its schedule (`CELERY_SCHEDULE_MODE=test` runs it every 4 minutes)

…and watch the message land in `http://localhost:8025`.

---

## 🌿 Branching strategy

- All backend work happens on a **feature branch** off `main`.
- `main` is **protected**: direct pushes are blocked locally (pre-push hook) and on GitHub (branch protection rule). Code reaches `main` only via pull request.
- Merging to `main` triggers:
  - `release.yml` — runs semantic-release, bumps the version in `back/pyproject.toml`, updates `CHANGELOG.md`, and tags a GitHub release.
  - `deploy-back.yml` — SSHes into DigitalOcean and pulls the new commit.

> Repo admins: enable the GitHub branch protection rule on `main` (require PR review + status checks `lint` and `Validate Conventional Commit format` to pass).

---

## 🧾 Commit & PR conventions (MANDATORY)

We follow [Conventional Commits](https://www.conventionalcommits.org/). The local `commit-msg` hook and the `Lint PR title` workflow both enforce this.

### Format

```
<type>(<scope>)?: <subject>
```

- `type` ∈ `feat | fix | perf | refactor | docs | style | test | build | ci | chore | revert`
- `scope` is optional but recommended (e.g. `alerts`, `auth`, `notifications`)
- `subject` starts with a letter, max 100 chars, no trailing period

### Examples

```text
feat(alerts): add wind-speed threshold endpoint
fix(auth): correct timezone conversion in JWT expiry
chore: bump uv lockfile
refactor(notifications): simplify alert calculation
docs: update backend setup instructions
```

### Release rules

`feat` → minor bump · `fix`/`perf` → patch bump · everything else → patch bump (except `chore(release)` which is skipped).

Squash-merging a PR uses the **PR title** as the squashed commit message — that's why the PR title is what semantic-release classifies on.

---

## 🛠️ Day-to-day commands

```bash
make install        # uv sync
make lint           # ruff check
make format         # ruff format (writes)
make format-check   # ruff format --check (CI)
make check          # lint + format-check (same gate the pre-push hook runs)
make test           # pytest, no-op until tests exist
```

The `pre-push` hook runs `make check` automatically. If it isn't green, the push is refused. To bypass in genuine emergencies: `PRE_PUSH_SKIP=1 git push ...`.

---

## 🤖 CI/CD

| Workflow | Trigger | Job |
| --- | --- | --- |
| `lint-pr-title.yml` | PR opened/edited | Validate the PR title is Conventional Commits |
| `ci.yml` | PR + push to `main` | `uv sync` → `ruff check` → `manage.py check` (format check enabled later, see `ci.yml` comment) |
| `release.yml` | push to `main` | semantic-release: tag, changelog, version bump |
| `deploy-back.yml` | push to `main` | SSH-deploy to DigitalOcean |
