#!/usr/bin/env python3
"""UserPromptSubmit hook: scan the user prompt for correction-phrase patterns.

On match, append a candidate record to ``.treadmill-local/learning-candidates.jsonl``
and emit an ``additionalContext`` injection so the orchestrator considers
authoring a learning.

The hook is advisory. It surfaces candidates; the orchestrator exercises
judgment. False positives are cheap; missed learnings are expensive.

Reads JSON from stdin per Claude Code spec:
    {"hook_event_name": "UserPromptSubmit",
     "prompt": "...",
     "session_id": "...",
     "transcript_path": "...",
     ... }

Writes JSON to stdout to control behavior:
    {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "..."
    }}

See docs/adrs/0008-learning-capture-skill-plus-hook-triggers.md.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Resolve paths relative to the repo root (this file's grandparent's parent).
REPO_ROOT = Path(__file__).resolve().parents[2]
TRIGGERS_PATH = REPO_ROOT / "tools" / "dev-hooks" / "learning_triggers.json"
QUEUE_PATH = REPO_ROOT / ".treadmill-local" / "learning-candidates.jsonl"
RATE_LIMIT_SECONDS = 60  # don't fire same matched phrase twice within this window


def _load_triggers() -> tuple[list[str], list[str]]:
    if not TRIGGERS_PATH.exists():
        return [], []
    data = json.loads(TRIGGERS_PATH.read_text())
    return data.get("phrases", []), data.get("false_positive_skips", [])


def _scan(prompt: str, phrases: list[str], skips: list[str]) -> str | None:
    """Return the first matched phrase, or None. Skips win over phrases."""
    lower = prompt.lower()
    for skip in skips:
        if skip.lower() in lower:
            return None
    for phrase in phrases:
        if phrase.lower() in lower:
            return phrase
    return None


def _was_recently_fired(matched: str) -> bool:
    """Cheap rate limit: don't surface the same trigger twice within RATE_LIMIT_SECONDS."""
    if not QUEUE_PATH.exists():
        return False
    cutoff = time.time() - RATE_LIMIT_SECONDS
    try:
        with QUEUE_PATH.open() as f:
            tail = f.readlines()[-50:]  # only check recent entries
    except OSError:
        return False
    for line in tail:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("matched") != matched:
            continue
        try:
            ts = datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        if ts.timestamp() >= cutoff:
            return True
    return False


def _append_candidate(matched: str, session_id: str, transcript_path: str) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "trigger": "correction-phrase",
        "matched": matched,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "status": "open",
    }
    with QUEUE_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Malformed input — exit silently so Claude Code keeps going.
        return 0

    prompt = payload.get("prompt", "")
    if not prompt:
        return 0

    phrases, skips = _load_triggers()
    matched = _scan(prompt, phrases, skips)
    if matched is None:
        return 0

    if _was_recently_fired(matched):
        return 0

    _append_candidate(
        matched=matched,
        session_id=payload.get("session_id", ""),
        transcript_path=payload.get("transcript_path", ""),
    )

    injection = (
        "[treadmill auto-capture] A correction-phrase trigger fired "
        f"(matched: {matched!r}). Consider whether this moment warrants a "
        "/learning capture before continuing — see "
        "docs/adrs/0008-learning-capture-skill-plus-hook-triggers.md. "
        f"Open candidates: cat {QUEUE_PATH.relative_to(REPO_ROOT)}"
    )

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": injection,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
