# Documentation Index

## Auto-loaded at session start (~1k tokens total)

- `CLAUDE.md` — entry point, sibling repos, read-first warnings
- `.claude/COMMON_MISTAKES.md` — top failure modes
- `.claude/QUICK_START.md` — make/docker/alembic commands
- `.claude/ARCHITECTURE_MAP.md` — current + target layout

## On-demand (read when relevant)

- `.claude/CONTINUE.md` — current handoff state. Read when user says "continue".
- `docs/flows/notifications.md` — alert / email / push notification flow
- `docs/flows/data-ingestion.md` — how sensor readings flow from device to DB (HTTP path)
- `docs/flows/mqtt-ingest.md` — the MQTT transport end-to-end (subscriber + agri-bridge publisher, topics, config, deploy, testing)
- `docs/flows/alerts.md` — alert rule evaluation + delivery
- `readme.md` — user-facing repo description
- `CHANGELOG.md` — release history

## In another location

- `~/.claude/skills/senior-dev/SKILL.md` — architecture playbook
  - `reference/{layout,tooling,settings,data,api,testing,delivery,release}.md`
  - `refactor/agri-api-plan.md` — 10-phase PR-by-PR rebuild plan

## Add-as-you-learn

- `docs/learnings/` — topic-specific docs (one per topic). Drop notes here
  when you discover something non-obvious that took >30 min to figure out.
- `docs/archive/` — old docs kept for history but never auto-loaded.

---

**Last Updated**: 2026-05-28
