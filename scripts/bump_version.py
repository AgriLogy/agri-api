"""Update the version field in back/pyproject.toml in place.

Called by semantic-release's @semantic-release/exec plugin during the
prepare step. Keeps things stdlib-only so semantic-release doesn't need
to set up a Python environment beyond the runner default.

Usage:
    python scripts/bump_version.py <new-version>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def bump(new_version: str) -> None:
    pyproject = Path(__file__).resolve().parent.parent / "back" / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    updated, n = re.subn(
        r'^version\s*=\s*"[^"]*"',
        f'version = "{new_version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise SystemExit(f"Could not find a version line to update in {pyproject}.")
    pyproject.write_text(updated, encoding="utf-8")
    print(f"bumped {pyproject} -> version = \"{new_version}\"")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/bump_version.py <new-version>")
    bump(sys.argv[1])
