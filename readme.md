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

```bash
cp back/env-example back/.env   # then fill in real values
make up                         # docker compose: postgres + redis + adminer + django
```

Adminer is on `:8080`, Django on `:8000`.

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
| `ci.yml` | PR + push to `main` | `uv sync` → `ruff check` → `ruff format --check` → `manage.py check` |
| `release.yml` | push to `main` | semantic-release: tag, changelog, version bump |
| `deploy-back.yml` | push to `main` | SSH-deploy to DigitalOcean |
