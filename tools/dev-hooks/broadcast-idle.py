#!/usr/bin/env python3
"""Stop hook: broadcast worker availability to the owning coordinator (ADR-0084).

When a worker session finishes responding and goes idle, this hook
drops an ``[AVAILABLE]`` relay file into the OWNING coordinator's inbox
(``~/.cc-channels/coordinator-<slug>/relay/``) and records the worker's
availability state under ``~/.treadmill/availability/<label>.json``.
The receiving coordinator's prompt §4 handles the signal by checking
for ready tasks and either re-briefing this worker or replying
"stand by".

Only WORKER labels broadcast (shape ``worker-<slug>-<n>``). Orchestrators
(``treadmill-<name>``), coordinators (``coordinator-*``), evaluators
(``evaluator-*``), and any other label class do NOT broadcast and return
immediately. Evaluators are intentionally excluded — they are
review-triggered per ADR-0090, not idle-task-assigned; extend to
``worker- OR evaluator-`` if a future model gives evaluators
coordinator-routed work.

The owning coordinator is derived by STRING SURGERY — not a positional
split (owner/repo slugs contain hyphens):

    strip ``worker-`` prefix → strip trailing ``-<digits>`` → prepend
    ``coordinator-``

    Examples:
      worker-medicoder-1            → coordinator-medicoder
      worker-medicoderhq-medicoder-2 → coordinator-medicoderhq-medicoder
      worker-joelepper-treadmill-3  → coordinator-joelepper-treadmill

If the owning coordinator inbox does not exist, the relay write is
SUPPRESSED (nothing written) rather than falling back to fan-out — a
missed assignment self-heals on the next cooldown tick; fan-out to
every coordinator is the wake-noise leak this change fixes (coordinator
reported 20+ non-actionable wakes from orchestrator idle ticks).

Opt-out is silent — the hook only acts when its environment + cooldown
preconditions hold:

- ``TREADMILL_SESSION_LABEL`` must be set (labeled session).
- The label must match ``worker-<slug>-<n>`` shape exactly.
- The last broadcast must be at least 3600s (1h) old. The original
  300s cooldown shipped in PR #264 was sized for fast-turn workers,
  but during long-haul autonomous-loop idle (30-min ScheduleWakeup
  ticks), every tick crossed the 300s window and fired a broadcast.
  Coordinator-medicoder flagged the noise after ~16 broadcasts in
  one overnight quiet period. A 1-hour floor suppresses tick-driven
  re-broadcasts while still letting a worker that genuinely just
  became available signal once. A proper edge-only protocol
  (broadcast only on busy → idle transitions) is the v2 fix.

State on disk:
- ``~/.treadmill/session-state/<label>/last-idle-broadcast`` — unix
  timestamp; presence + age gate the cooldown.
- ``~/.treadmill/availability/<label>.json`` — current availability
  record. Coordinators may read this for liveness; we maintain it
  per-broadcast so the most recent value is always fresh.

Output: the owning coordinator inbox gets a file named
``<ns_ts>-available-from-<label>.md`` with body::

  [AVAILABLE]

  [from: <label>]

  Worker <label> is idle and available for task assignment.

Stdlib only; no cc-relay subprocess. Stop hooks must always exit 0;
transient I/O failures are swallowed so a missing dir or full disk
doesn't block the worker from idling.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

_COOLDOWN_SECONDS = 3600

# worker-<slug>-<n> shape — slug must be non-empty and index must be digits
_WORKER_LABEL_RE = re.compile(r"^worker-.+-\d+$")


def _now_unix() -> int:
    return int(time.time())


def _is_worker_label(label: str) -> bool:
    """True only for ``worker-<slug>-<n>`` shapes.

    Orchestrators (``treadmill-*``), coordinators (``coordinator-*``),
    evaluators (``evaluator-*``), and bare worker strings without a
    numeric index all return False.
    """
    return bool(_WORKER_LABEL_RE.match(label))


def _owning_coordinator_label(label: str) -> str | None:
    """Derive the owning coordinator label from a worker label.

    String surgery (not a positional split — slugs contain hyphens):
    strip ``worker-`` prefix, strip trailing ``-<digits>`` index,
    prepend ``coordinator-``. Returns ``None`` when the label doesn't
    have a trailing numeric index (not a valid worker label shape).
    """
    if not label.startswith("worker-"):
        return None
    without_prefix = label[len("worker-"):]
    stripped = re.sub(r"-\d+$", "", without_prefix)
    if not stripped or stripped == without_prefix:
        return None
    return f"coordinator-{stripped}"


def _cooldown_active(state_path: Path) -> bool:
    if not state_path.exists():
        return False
    try:
        ts = int(state_path.read_text().strip())
    except (ValueError, OSError):
        return False
    return (_now_unix() - ts) < _COOLDOWN_SECONDS


def _write_cooldown(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(f"{_now_unix()}\n")


def _write_availability(home: Path, label: str) -> None:
    record = {
        "label": label,
        "available_since": _now_unix(),
        "updated_at": _now_unix(),
    }
    target = home / ".treadmill" / "availability" / f"{label}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, sort_keys=True) + "\n")


def _broadcast_to_owning_coordinator(home: Path, label: str) -> None:
    """Write an ``[AVAILABLE]`` relay file into the owning coordinator's
    inbox only. If the inbox does not exist, suppresses silently."""
    coord_label = _owning_coordinator_label(label)
    if coord_label is None:
        return
    inbox = home / ".cc-channels" / coord_label / "relay"
    if not inbox.exists():
        return
    body = (
        "[AVAILABLE]\n\n"
        f"[from: {label}]\n\n"
        f"Worker {label} is idle and available for task assignment.\n"
    )
    try:
        ns = time.time_ns()
        tag = secrets.token_hex(2)
        out = inbox / f"{ns}-{tag}-available-from-{label}.md"
        out.write_text(body)
    except OSError:
        pass


def main() -> int:
    label = os.environ.get("TREADMILL_SESSION_LABEL", "").strip()
    if not label:
        return 0
    if not _is_worker_label(label):
        return 0

    home = Path(os.environ.get("HOME", "")).expanduser()
    if not str(home):
        return 0

    state_path = (
        home / ".treadmill" / "session-state" / label / "last-idle-broadcast"
    )
    if _cooldown_active(state_path):
        return 0

    try:
        _write_cooldown(state_path)
        _write_availability(home, label)
        _broadcast_to_owning_coordinator(home, label)
    except OSError:
        # Disk full / permission denied / etc. Don't block the worker.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
