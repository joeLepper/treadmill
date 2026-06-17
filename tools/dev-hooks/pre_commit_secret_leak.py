#!/usr/bin/env python3
"""Git pre-commit hook: block staged content that leaks client names / IDs.

The PUBLIC joeLepper/treadmill repo must not carry real client names or
the deployment account-id. This hook scans the ADDED lines of the staged
diff against a denylist and refuses the commit on a match.

OUT-OF-SOURCE-CONTROL denylist
------------------------------
The denylist is loaded at runtime from the operator-local
``~/.treadmill/codenames.json`` (override ``TREADMILL_CODENAMES_FILE``) —
the SAME file the obsidian secret-leak gate uses. This script contains
NO literals. A clone without that file scans against an empty denylist
and allows the commit (a public contributor has no client secrets to
leak); the operator's checkout has the file and is gated.

Scope: only ADDED lines (``+`` in the staged diff), so the hook stops
NEW leaks and does not choke on the pre-existing backlog (the historical
scrub + a filter-repo pass handle that separately).

Bypass (only for a genuine false positive): ``git commit --no-verify``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def load_denylist() -> list[str]:
    """Load the denylist from the out-of-SC codenames file; [] if absent."""
    path = Path(
        os.environ.get("TREADMILL_CODENAMES_FILE")
        or Path.home() / ".treadmill" / "codenames.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    denylist = data.get("denylist", [])
    if not isinstance(denylist, list):
        return []
    return [s for s in denylist if isinstance(s, str) and s]


def added_lines(diff_text: str) -> list[tuple[str, str]]:
    """Parse a unified staged diff → list of (path, added_line_text).

    Only lines starting with a single ``+`` (additions), not the
    ``+++ b/path`` file headers.
    """
    current: str | None = None
    out: list[tuple[str, str]] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/"):]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((current or "?", line[1:]))
    return out


def find_hits(
    added: list[tuple[str, str]], denylist: list[str]
) -> list[tuple[str, str, str]]:
    """Return (path, token, line) for each denylist token found in an
    added line. Pure; the unit-testable core."""
    hits: list[tuple[str, str, str]] = []
    for path, text in added:
        for token in denylist:
            if token and token in text:
                hits.append((path, token, text))
    return hits


def _staged_diff() -> str:
    return subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--no-color"],
        capture_output=True, text=True, check=False,
    ).stdout


def main() -> int:
    denylist = load_denylist()
    if not denylist:
        return 0  # no out-of-SC file → nothing to gate (public clone)
    hits = find_hits(added_lines(_staged_diff()), denylist)
    if not hits:
        return 0
    print(
        "✖ pre-commit: blocked — staged content contains "
        "client-sensitive tokens. Codename them per the convention "
        "(see ~/.treadmill/codenames.json):",
        file=sys.stderr,
    )
    seen: set[tuple[str, str]] = set()
    for path, token, text in hits:
        if (path, token) in seen:
            continue
        seen.add((path, token))
        print(f"  {path}: '{token}' in: …{text.strip()[:80]}…", file=sys.stderr)
    print(
        "  Fix the names, or bypass only for a true false positive: "
        "git commit --no-verify",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
