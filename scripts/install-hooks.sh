#!/usr/bin/env bash
# One-shot bootstrap: tell git to use the in-repo hooks under .githooks/.
# Run once after cloning. Re-run after pulling new hooks.

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

git config core.hooksPath .githooks
chmod +x .githooks/*

echo "✓ Git hooks installed (core.hooksPath = .githooks)."
echo "  - commit-msg: enforces Conventional Commits"
echo "  - pre-push:   blocks direct pushes to main + runs ruff check/format"
