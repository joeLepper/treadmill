"""Tests for the Stop-hook session-end candidate sweep.

The hook reads ``.treadmill-local/learning-candidates.jsonl`` and emits
a Claude Code ``additionalContext`` injection when open entries remain.
We exercise the script as a subprocess (the way Claude Code invokes it)
and inspect its stdout. Path is steered via ``TREADMILL_CANDIDATES_FILE``.

See ``docs/plans/2026-05-11-week-2-closure.md`` work item D.11.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "review_candidates_at_stop.py"


def _run(candidates_path: Path | str) -> subprocess.CompletedProcess[str]:
    """Invoke the hook with TREADMILL_CANDIDATES_FILE pointed at the
    given path. Returns the completed process (stdout/stderr captured)."""
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input="",
        env={
            "TREADMILL_CANDIDATES_FILE": str(candidates_path),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def test_empty_file_emits_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")

    proc = _run(empty)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_no_open_candidates_emits_nothing(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    _write_jsonl(
        queue,
        [
            {"matched": "i don't think", "status": "captured"},
            {"matched": "that's wrong", "status": "captured"},
            {"matched": "no actually", "status": "dismissed"},
        ],
    )

    proc = _run(queue)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_one_open_candidate_emits_additional_context(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    _write_jsonl(
        queue,
        [
            {"matched": "you misunderstood", "status": "captured"},
            {"matched": "I actually meant", "status": "open"},
        ],
    )

    proc = _run(queue)

    assert proc.returncode == 0
    assert proc.stdout != ""
    out = json.loads(proc.stdout)
    # Per Claude Code's Stop-hook schema, the operator-facing channel is
    # ``systemMessage`` — ``hookSpecificOutput.additionalContext`` is only
    # valid for UserPromptSubmit / PostToolUse / PostToolBatch.
    context = out["systemMessage"]
    assert "1 open" in context
    # The slug for "I actually meant" lower-cases + hyphenates.
    assert "i-actually-meant" in context


def test_missing_file_emits_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.jsonl"

    proc = _run(missing)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_multiple_open_candidates_lists_each_slug(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    _write_jsonl(
        queue,
        [
            {"matched": "i don't think", "status": "open"},
            {"matched": "that's wrong!", "status": "open"},
            {"matched": "already-resolved", "status": "captured"},
        ],
    )

    proc = _run(queue)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    context = out["systemMessage"]
    assert "2 open" in context
    assert "i-don-t-think" in context
    assert "that-s-wrong" in context


def test_malformed_lines_skipped_not_fatal(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        "not json at all\n"
        + json.dumps({"matched": "real one", "status": "open"})
        + "\n"
        + "{bad json\n"
    )

    proc = _run(queue)

    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert "1 open" in out["systemMessage"]
