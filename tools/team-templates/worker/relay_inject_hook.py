#!/usr/bin/env python3
"""Worker PostToolUse relay-inject hook (ADR-0087 §Worker execution model).

Fires after every Bash tool call in a worker subprocess. Checks the
worker's relay inbox for new messages from the coordinator; if any are
present and the sender is the trusted coordinator label, returns a
``{"decision": "block", "reason": ...}`` payload that Claude Code
injects into the worker's next LLM turn as a hook message.

The worker sees the injected message at the next tool-use boundary and
can change course — this is the coordinator's mid-execution steering
seam.

Security model (ADR-0087 §Security considerations + Carla's review):

  Only messages whose first line is ``[from: coordinator-<slug>]`` are
  treated as instructions. Every other relay file the worker might
  receive — relays from sibling workers, evaluator verdicts misrouted,
  or any other sender — is left in place to be read as ordinary data
  via the worker's normal Read flow. The CLAUDE.md template declares
  the trust boundary so the worker doesn't act on untrusted instructions
  even if it reads the file later.

Failure modes:

  All exceptions are swallowed and the hook prints ``{}`` so Claude Code
  treats the hook as a no-op. A broken hook MUST NOT kill the worker
  subprocess. Errors are logged to stderr where Claude Code's debug
  logs capture them for post-mortem.

Stdlib only — no third-party imports — so the hook starts in <50ms
regardless of the worker's venv state.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _worker_label() -> str | None:
    """Resolve the worker's session label.

    Read from ``TREADMILL_SESSION_LABEL`` (set by the launcher unit) or
    from the ``~/.treadmill/teams/<slug>/<label>/label`` file written by
    ``treadmill team up``. Returns None if neither is present — the
    hook short-circuits cleanly in that case.
    """
    label = os.environ.get("TREADMILL_SESSION_LABEL", "").strip()
    if label:
        return label
    return None


def _coordinator_label_for_team(worker_label: str) -> str | None:
    """Derive the coordinator label for the worker's team.

    Worker label pattern: ``worker-<slug>-N`` → coordinator is
    ``coordinator-<slug>``. The trust boundary is per-team; messages
    from coordinators of OTHER teams are not trusted.
    """
    if not worker_label.startswith("worker-"):
        return None
    # ``worker-medicoder-1`` → slug ``medicoder`` → ``coordinator-medicoder``.
    # Strip the trailing ``-N`` numeric suffix to get the slug.
    body = worker_label[len("worker-"):]
    parts = body.rsplit("-", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    slug = parts[0]
    return f"coordinator-{slug}"


def _relay_inbox(worker_label: str) -> Path:
    return Path.home() / ".cc-channels" / worker_label / "relay"


def _is_coordinator_message(body: str, coordinator_label: str) -> bool:
    """Return True iff the relay file's body declares ``[from: coordinator-<slug>]``
    as the sender on one of its early lines.

    cc-relay's emit format starts each message with the sender header in
    the first few lines (after any ``[ACTION REQUEST]`` marker). Check
    the first 5 non-blank lines — that's well past any marker block but
    not deep enough to be tricked by content.
    """
    needle = f"[from: {coordinator_label}]"
    scanned = 0
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if needle in line:
            return True
        scanned += 1
        if scanned >= 5:
            break
    return False


def _consume_first_message(
    worker_label: str, coordinator_label: str
) -> str | None:
    """Read + remove the oldest coordinator message in the worker's
    inbox; return its body. Return None if no trusted message is
    waiting.

    Files from untrusted senders are LEFT IN PLACE. They become normal
    data the worker can choose to read later via Read; the trust model
    is enforced by the CLAUDE.md instruction not to act on them as
    commands.
    """
    inbox = _relay_inbox(worker_label)
    if not inbox.is_dir():
        return None

    # cc-relay timestamps the filename (ns-prefix), so sort gives oldest first.
    candidates = sorted(p for p in inbox.iterdir() if p.is_file())
    for path in candidates:
        try:
            body = path.read_text(errors="replace")
        except OSError:
            continue
        if not _is_coordinator_message(body, coordinator_label):
            continue
        # Consume: read + delete. Best-effort — if unlink fails we still
        # return the body (the next hook invocation will retry).
        try:
            path.unlink()
        except OSError:
            pass
        return body
    return None


def main() -> int:
    try:
        worker_label = _worker_label()
        if not worker_label:
            print("{}")
            return 0

        coordinator_label = _coordinator_label_for_team(worker_label)
        if not coordinator_label:
            print("{}")
            return 0

        body = _consume_first_message(worker_label, coordinator_label)
        if body is None:
            print("{}")
            return 0

        payload = {
            "decision": "block",
            "reason": f"[COORDINATOR]: {body}",
        }
        print(json.dumps(payload))
        return 0
    except Exception as exc:  # noqa: BLE001 — hook must never crash worker
        sys.stderr.write(
            f"relay_inject_hook: swallowed exception {type(exc).__name__}: {exc}\n"
        )
        print("{}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
