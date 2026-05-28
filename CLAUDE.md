# CLAUDE.md — agri-api

Quick-start guide for Claude Code. **Read this in full; everything else is
on-demand.**

> **Note:** this repo will be renamed `agri-api` → `agri-api` (the
> role is "API service", not "backend"). Until the rename PR lands, paths
> below still say `agri-api`.

## What this repo is

The Agrilogy HTTP API service. Django 4.2 + DRF + Celery + Redis. Talks
to Supabase Postgres (schema owned by sibling repo `agri-db`).

**Tech:** Django · DRF · drf-yasg · Celery · Redis · uv · Docker Compose

## Sibling repos

| Repo | Path | Role |
|---|---|---|
| `agri-db` | `../agri-db/` | Postgres schema-of-record (Alembic). Owns migrations. |
| `agri-core` | `../agri-core/` | (Planned) Pure-Python shared lib: device adapters + calc engine. |
| `agri-front` | `../agri-front/` | Web app. |

## ⚠️ Read first

Three things that will bite if you skip them:

1. **Schema lives in `agri-db`.** Never run `python manage.py migrate`
   or `makemigrations` here. Django's entrypoint already skips migrate
   on boot (`back/docker-entrypoint.sh:75-78`). See `.claude/COMMON_MISTAKES.md`.
2. **Bootstrap order:** `cd ../agri-db && make upgrade-dev` BEFORE
   `make up` here. Empty Supabase → schema first, then start the app.
3. **Commit rules:** local machine only (never over SSH on the droplet);
   no `Co-Authored-By` trailer; every PR pairs with an issue;
   use `mks-zakaria` gh account. Stored in user memory; surfaced in
   `.claude/COMMON_MISTAKES.md`.

## On-demand docs (load when relevant)

- `.claude/COMMON_MISTAKES.md` — the top-5 critical things, with fixes.
- `.claude/QUICK_START.md` — common Make / Docker / Alembic commands.
- `.claude/ARCHITECTURE_MAP.md` — current layout + target.
- `.claude/CONTINUE.md` — current handoff state. **Read when user says
  "continue".** Per memory note `continue-handoff`.
- `docs/INDEX.md` — links into deeper docs (`docs/flows/*` for
  notifications / data-ingestion / alerts).

## For architecture / refactor questions

Don't reinvent here. The user has a `senior-dev` skill at
`~/.claude/skills/senior-dev/` that contains:

- 8 reference docs (layout, tooling, settings, data, api, testing,
  delivery, release).
- A **10-phase PR-by-PR refactor plan** for the rebuild.

Invoke via `/senior-dev` or by mentioning architecture/refactor topics.

## Quick commands

```bash
make bootstrap        # first time: uv sync + install git hooks
make up               # start dev stack (Postgres on Supabase, redis + django + celery local)
make down
make test
make lint format-check
```

Migration commands run from `../agri-db/`:
```bash
cd ../agri-db
make upgrade-dev      # apply pending Alembic migrations to Supabase dev
make current-dev      # show current head
```

## At task completion

- New "completion" docs (decisions, post-mortems worth keeping) go in
  `.claude/completions/YYYY-MM-DD-name.md` — not auto-loaded.
- Don't auto-create files in `.claude/sessions/` unless you're handing
  off mid-task.

---

Optimized with [claude-token-optimizer](https://github.com/nadimtuhin/claude-token-optimizer).
