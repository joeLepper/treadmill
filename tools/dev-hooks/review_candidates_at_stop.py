#!/usr/bin/env python3
"""Stop hook: surface any open learning candidates before a session ends.

ADR-0008 ships an auto-capture skill that drops candidate entries into
``.treadmill-local/learning-candidates.jsonl`` on correction-phrase
matches. The ADR's follow-up flagged a risk: candidates accumulate
silently if the operator forgets to sweep before stopping. This hook
closes the loop — when the session is about to end, we read the queue,
count entries still ``status == "open"``, and surface a summary to the
operator via the Stop-hook ``systemMessage`` field. The orchestrator
then has the cue (and the slugs) to either author a ``/learning`` for
each or dismiss them with a ``notes`` entry.

Note on payload shape: Stop hooks must conform to Claude Code's
hook-output schema, which restricts ``hookSpecificOutput.additionalContext``
to ``UserPromptSubmit`` / ``PostToolUse`` / ``PostToolBatch``. For
Stop hooks the valid surfacing channel is ``systemMessage``.

Reads no stdin — Claude Code invokes Stop hooks with an event payload
on stdin, but we ignore it; the only relevant state is the queue file.
On any unexpected exception we log to stderr and exit 0: a hook bug
must never block the session from ending.

The file path is overridable via ``TREADMILL_CANDIDATES_FILE`` so the
test suite can point at a temp file. Default is the repo-relative
``.treadmill-local/learning-candidates.jsonl``.

See docs/adrs/0008-learning-capture-skill-plus-hook-triggers.md and
docs/plans/2026-05-11-week-2-closure.md work item D.11.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Resolve the repo root from this file's location so the default path
# works whether the hook is invoked via absolute path or via $CLAUDE_PROJECT_DIR.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATES_PATH = REPO_ROOT / ".treadmill-local" / "learning-candidates.jsonl"


def _candidates_path() -> Path:
    override = os.environ.get("TREADMILL_CANDIDATES_FILE")
    if override:
        return Path(override)
    return DEFAULT_CANDIDATES_PATH


def _slug(matched: str) -> str:
    """Compact a ``matched`` phrase into a short identifier for the
    additional-context message. Lower-cased, non-alphanumerics collapsed
    to hyphens, trimmed to ~24 chars."""
    out: list[str] = []
    prev_dash = False
    for ch in matched.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")
    return slug[:24] or "candidate"


def _open_candidates(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    open_entries: list[dict[str, object]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Skip malformed lines — they don't block the sweep.
                continue
            if rec.get("status") == "open":
                open_entries.append(rec)
    return open_entries


def main() -> int:
    try:
        # Drain stdin (Stop hook payload) but don't require it.
        try:
            sys.stdin.read()
        except (OSError, ValueError):
            pass

        path = _candidates_path()
        open_entries = _open_candidates(path)
        if not open_entries:
            return 0

        slugs = ", ".join(_slug(str(rec.get("matched", ""))) for rec in open_entries)
        message = (
            f"[treadmill stop-hook] {len(open_entries)} open learning candidates remain. "
            f"Sweep before ending. Candidate slugs: {slugs}. Open via /learning or "
            f"skim with: cat .treadmill-local/learning-candidates.jsonl"
        )
        json.dump({"systemMessage": message}, sys.stdout)
        return 0
    except Exception as exc:  # noqa: BLE001 — hook must never block
        print(f"[review_candidates_at_stop] error: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
