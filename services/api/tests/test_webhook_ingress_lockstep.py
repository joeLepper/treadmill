"""CI gate: no direct Event-row writes outside the webhook-ingress helper.

Per ADR-0063, all webhook-sourced Event rows MUST flow through
``treadmill_api.webhooks.persist.persist_and_resolve_webhook_event``.
That helper is the single FK-resolution + buffer-on-miss + publish seam;
bypassing it creates the dual-ingress drift hazard that caused the 2026-05-29
Task 3b 40-minute stall.

This test scans the two directories closest to ingress — routers and
coordination — for direct Event-row writes and fails if it finds any outside
the curated allowlist of pre-existing, legitimate non-webhook writers.

Allowlisted sites (one entry per path, with a one-line reason):
  treadmill_api/coordination/consumer.py
      _persist_event: replays worker-origin events via ON CONFLICT DO NOTHING
  treadmill_api/coordination/triggers.py
      lifecycle event publisher: review.override + validate.override (ADR-0042)

NOT scanned (outside the scope of this gate):
  treadmill_api/webhooks/persist.py  — the approved seam itself
  treadmill_api/dispatch.py          — task/step lifecycle dispatcher
"""
from __future__ import annotations

import pathlib
import re
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_ROOT = pathlib.Path(__file__).parent.parent

_SCAN_DIRS = [
    _API_ROOT / "treadmill_api" / "routers",
    _API_ROOT / "treadmill_api" / "coordination",
]

# Patterns that constitute a direct Event-row write.
_WRITE_PATTERNS = re.compile(
    r"session\.add\(Event\(|pg_insert\(Event\)|insert\(Event\)|session\.merge\(Event\("
)

# Allowlisted paths (relative to services/api/) and their one-line reasons.
# Add a new entry here only for legitimate non-webhook writers that existed
# before ADR-0063 was enforced.  New webhook ingress paths are NOT eligible.
_ALLOWLIST: dict[str, str] = {
    # lifecycle event publisher: review.override + validate.override (ADR-0042)
    "treadmill_api/coordination/triggers.py": (
        "lifecycle event publisher: review.override + validate.override (ADR-0042)"
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Hit(NamedTuple):
    rel_path: str
    lineno: int
    text: str


def _scan() -> list[_Hit]:
    hits: list[_Hit] = []
    for scan_dir in _SCAN_DIRS:
        for py_file in sorted(scan_dir.glob("*.py")):
            rel_path = py_file.relative_to(_API_ROOT).as_posix()
            for lineno, line in enumerate(py_file.read_text().splitlines(), start=1):
                if _WRITE_PATTERNS.search(line):
                    hits.append(_Hit(rel_path=rel_path, lineno=lineno, text=line.strip()))
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_direct_event_writes_outside_allowlist() -> None:
    """Fail if routers/ or coordination/ contain direct Event-row writes.

    New webhook ingress paths must delegate to
    ``treadmill_api.webhooks.persist.persist_and_resolve_webhook_event``
    (ADR-0063 lock-step requirement).  Only the sites in ``_ALLOWLIST`` may
    write Event rows directly; everything else is a violation.
    """
    hits = _scan()

    violations: list[str] = []
    for hit in hits:
        if hit.rel_path not in _ALLOWLIST:
            violations.append(f"  {hit.rel_path}:{hit.lineno}  {hit.text}")

    assert not violations, (
        "Direct Event-row write(s) found outside the ADR-0063 allowlist.\n"
        "New webhook ingress paths must call\n"
        "  treadmill_api.webhooks.persist.persist_and_resolve_webhook_event\n"
        "instead of writing Event rows directly.\n\n"
        "To add a legitimate non-webhook writer, extend _ALLOWLIST in this file\n"
        "with a one-line comment naming the reason.\n\n"
        "Offending site(s):\n" + "\n".join(violations)
    )


def test_allowlist_entries_still_contain_event_writes() -> None:
    """Ensure every allowlisted file still contains at least one Event write.

    A passing allowlist entry that no longer has any matching writes signals
    that the file was refactored and the allowlist is stale — remove the
    entry so the gate stays tight.
    """
    hits = _scan()
    hits_by_path: dict[str, list[_Hit]] = {}
    for hit in hits:
        hits_by_path.setdefault(hit.rel_path, []).append(hit)

    stale: list[str] = []
    for path in _ALLOWLIST:
        if path not in hits_by_path:
            stale.append(f"  {path}  (no Event writes found — remove from _ALLOWLIST)")

    assert not stale, (
        "Stale allowlist entry/entries — the file no longer contains direct "
        "Event-row writes.  Remove the entry from _ALLOWLIST in this test to "
        "keep the gate tight.\n\n"
        "Stale entry/entries:\n" + "\n".join(stale)
    )
