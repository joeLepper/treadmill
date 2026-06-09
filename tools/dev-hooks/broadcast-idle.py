#!/usr/bin/env python3
"""Stop hook: broadcast worker availability to coordinators (ADR-0084).

When a worker session finishes responding and goes idle, this hook
drops an ``[AVAILABLE]`` relay file into every coordinator inbox under
``~/.cc-channels/coordinator-*/relay/`` and records the worker's
availability state under ``~/.treadmill/availability/<label>.json``.
The receiving coordinator's prompt §4 handles the signal by checking
for ready tasks and either re-briefing this worker or replying
"stand by".

Opt-out is silent — the hook only acts when its environment + cooldown
preconditions hold:

- ``TREADMILL_SESSION_LABEL`` must be set (labeled session).
- The label must NOT start with ``coordinator-`` (coordinators don't
  broadcast availability).
- The last broadcast must be at least 300s old (cooldown). Fast
  multi-turn responses don't flood coordinator inboxes.

State on disk:
- ``~/.treadmill/session-state/<label>/last-idle-broadcast`` — unix
  timestamp; presence + age gate the cooldown.
- ``~/.treadmill/availability/<label>.json`` — current availability
  record. Coordinators may read this for liveness; we maintain it
  per-broadcast so the most recent value is always fresh.

Output: each coordinator inbox gets a file named
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
import secrets
import sys
import time
from pathlib import Path

_COOLDOWN_SECONDS = 300


def _now_unix() -> int:
    return int(time.time())


def _is_coordinator_label(label: str) -> bool:
    return label.startswith("coordinator-")


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


def _broadcast_to_coordinators(home: Path, label: str) -> None:
    """Write an ``[AVAILABLE]`` relay file into each discovered
    coordinator's inbox. Coordinators are detected by glob over
    ``~/.cc-channels/coordinator-*/relay/``."""
    channels_root = home / ".cc-channels"
    if not channels_root.exists():
        return
    body = (
        "[AVAILABLE]\n\n"
        f"[from: {label}]\n\n"
        f"Worker {label} is idle and available for task assignment.\n"
    )
    for entry in channels_root.iterdir():
        if not entry.is_dir() or not entry.name.startswith("coordinator-"):
            continue
        relay_dir = entry / "relay"
        try:
            relay_dir.mkdir(parents=True, exist_ok=True)
            ns = time.time_ns()
            tag = secrets.token_hex(2)
            out = relay_dir / f"{ns}-{tag}-available-from-{label}.md"
            out.write_text(body)
        except OSError:
            # One coordinator inbox is unwriteable; don't let it block
            # the rest. Stop-hook contract is exit-0-on-error.
            continue


def main() -> int:
    label = os.environ.get("TREADMILL_SESSION_LABEL", "").strip()
    if not label:
        return 0
    if _is_coordinator_label(label):
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
        _broadcast_to_coordinators(home, label)
    except OSError:
        # Disk full / permission denied / etc. Don't block the worker.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
