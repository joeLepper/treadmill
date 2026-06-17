"""Tests for the broadcast-idle Stop hook (ADR-0084).

Pins the skip conditions + the broadcast shape post-task-b71be765
(owning-coordinator scoping):

- TREADMILL_SESSION_LABEL unset → no-op
- Orchestrator label (treadmill-*) → no-op
- Coordinator label (coordinator-*) → no-op
- Evaluator label (evaluator-*) → no-op
- Worker label, cooldown active (< 3600s) → no-op
- Worker label, first broadcast → writes availability JSON + relay file
  in the OWNING coordinator's inbox ONLY (not a fan-out to all
  coordinator inboxes)
- Hyphenated owner/repo: worker-ramjac-ramjac-2 →
  coordinator-ramjac-ramjac (string surgery, not positional split)
- Owning coordinator inbox missing → relay suppressed, availability still
  written (self-heals on next cooldown tick; fan-out is the leak fixed)
- Second broadcast within 3600s → skipped (cooldown)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "broadcast-idle.py"


def _run(*, home: Path, label: str | None) -> subprocess.CompletedProcess[str]:
    env = {"HOME": str(home), "PATH": ""}
    if label is not None:
        env["TREADMILL_SESSION_LABEL"] = label
    return subprocess.run(
        [sys.executable, str(HOOK)],
        env=env, capture_output=True, text=True, timeout=10,
    )


def _seed_coordinator_inbox(home: Path, slug: str) -> Path:
    """Create ``~/.cc-channels/coordinator-<slug>/relay/``. Returns the relay path."""
    relay = home / ".cc-channels" / f"coordinator-{slug}" / "relay"
    relay.mkdir(parents=True)
    return relay


# ── skip conditions ──────────────────────────────────────────────────


def test_unset_label_is_noop(tmp_path: Path) -> None:
    """Without ``TREADMILL_SESSION_LABEL``, the hook returns 0 and
    writes nothing — Stop hooks fire on every session, but only labeled
    worker sessions broadcast."""
    home = tmp_path / "home"
    home.mkdir()
    _seed_coordinator_inbox(home, "ramjac")

    result = _run(home=home, label=None)

    assert result.returncode == 0
    relay_files = list(
        (home / ".cc-channels" / "coordinator-ramjac" / "relay").glob("*.md")
    )
    assert relay_files == []
    assert not (home / ".treadmill" / "availability").exists()


def test_coordinator_label_is_noop(tmp_path: Path) -> None:
    """A ``coordinator-*`` label short-circuits — coordinators don't
    broadcast availability to other coordinators."""
    home = tmp_path / "home"
    home.mkdir()
    relay = _seed_coordinator_inbox(home, "ramjac")

    result = _run(home=home, label="coordinator-ramjac")

    assert result.returncode == 0
    assert list(relay.glob("*.md")) == []
    assert not (home / ".treadmill" / "availability").exists()


def test_orchestrator_label_is_noop(tmp_path: Path) -> None:
    """Orchestrator labels (``treadmill-<name>``) must NOT broadcast.
    task b71be765: orchestrator idle ticks were waking every coordinator
    (fan-out) with no actionable signal — 20+ spurious wakes reported."""
    home = tmp_path / "home"
    home.mkdir()
    relay = _seed_coordinator_inbox(home, "ramjac")

    result = _run(home=home, label="treadmill-alan")

    assert result.returncode == 0
    assert list(relay.glob("*.md")) == []
    assert not (home / ".treadmill" / "availability").exists()


def test_evaluator_label_is_noop(tmp_path: Path) -> None:
    """Evaluator labels (``evaluator-*``) must NOT broadcast — evaluators
    are review-triggered per ADR-0090, not idle-task-assigned."""
    home = tmp_path / "home"
    home.mkdir()
    relay = _seed_coordinator_inbox(home, "ramjac")

    result = _run(home=home, label="evaluator-ramjac")

    assert result.returncode == 0
    assert list(relay.glob("*.md")) == []
    assert not (home / ".treadmill" / "availability").exists()


# ── worker broadcasts to owning coordinator only ─────────────────────


def test_worker_broadcasts_to_owning_coordinator_inbox(tmp_path: Path) -> None:
    """On first broadcast, a worker writes the availability JSON + drops
    a relay file into ITS OWNING coordinator's inbox only. Other
    coordinator inboxes are NOT written to."""
    home = tmp_path / "home"
    home.mkdir()
    owning_relay = _seed_coordinator_inbox(home, "ramjac")
    # Sibling coordinator inbox that must NOT receive a file.
    bystander_relay = _seed_coordinator_inbox(home, "other-team")

    result = _run(home=home, label="worker-ramjac-1")

    assert result.returncode == 0

    # Owning inbox: exactly one relay file with the right shape.
    files = list(owning_relay.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert body.startswith("[AVAILABLE]\n\n")
    assert "[from: worker-ramjac-1]" in body
    assert "Worker worker-ramjac-1 is idle" in body
    assert files[0].name.endswith("-available-from-worker-ramjac-1.md")

    # Bystander inbox: nothing written.
    assert list(bystander_relay.glob("*.md")) == []

    # Availability JSON written.
    avail = home / ".treadmill" / "availability" / "worker-ramjac-1.json"
    assert avail.exists()
    record = json.loads(avail.read_text())
    assert record["label"] == "worker-ramjac-1"
    assert isinstance(record["available_since"], int)
    assert isinstance(record["updated_at"], int)

    # Cooldown stamp written.
    cooldown = (
        home / ".treadmill" / "session-state" / "worker-ramjac-1"
        / "last-idle-broadcast"
    )
    assert cooldown.exists()


def test_hyphenated_owner_repo_resolved_correctly(tmp_path: Path) -> None:
    """String surgery (not positional split) resolves the owning coordinator
    even when the slug contains hyphens.

    worker-ramjac-ramjac-2  →  coordinator-ramjac-ramjac
    (a split('-')[1:3] slice would wrongly yield coordinator-ramjac)
    """
    home = tmp_path / "home"
    home.mkdir()
    owning_relay = _seed_coordinator_inbox(home, "ramjac-ramjac")
    wrong_relay = _seed_coordinator_inbox(home, "ramjac")

    result = _run(home=home, label="worker-ramjac-ramjac-2")

    assert result.returncode == 0
    # Correct coordinator (full slug) gets the file.
    assert len(list(owning_relay.glob("*.md"))) == 1
    # The naively-split coordinator does NOT get a file.
    assert list(wrong_relay.glob("*.md")) == []


def test_multi_coordinator_dirs_only_owning_receives(tmp_path: Path) -> None:
    """With multiple coordinator inboxes present, the hook writes to the
    owning coordinator's inbox ONLY — one file total, not N."""
    home = tmp_path / "home"
    home.mkdir()
    owning = _seed_coordinator_inbox(home, "ramjac")
    sibling_a = _seed_coordinator_inbox(home, "treadmill")
    sibling_b = _seed_coordinator_inbox(home, "acme")

    result = _run(home=home, label="worker-ramjac-1")
    assert result.returncode == 0

    assert len(list(owning.glob("*.md"))) == 1
    assert list(sibling_a.glob("*.md")) == []
    assert list(sibling_b.glob("*.md")) == []


# ── missing / absent coordinator inbox ──────────────────────────────


def test_owning_coordinator_inbox_missing_suppresses_relay(tmp_path: Path) -> None:
    """When the owning coordinator's inbox dir doesn't exist, the relay
    write is suppressed entirely (nothing written, no fan-out fallback).
    Availability + cooldown ARE still recorded — a missed assignment
    self-heals on the next cooldown tick."""
    home = tmp_path / "home"
    home.mkdir()
    # No coordinator-ramjac inbox present.
    (home / ".cc-channels").mkdir()

    result = _run(home=home, label="worker-ramjac-1")

    assert result.returncode == 0
    # No relay file anywhere.
    relay_files = list((home / ".cc-channels").rglob("*.md"))
    assert relay_files == []
    # Availability + cooldown still written.
    assert (home / ".treadmill" / "availability" / "worker-ramjac-1.json").exists()
    assert (
        home / ".treadmill" / "session-state" / "worker-ramjac-1"
        / "last-idle-broadcast"
    ).exists()


def test_channels_root_missing_suppresses_relay(tmp_path: Path) -> None:
    """When ``~/.cc-channels`` does not exist (fresh install), the relay
    write is suppressed. Availability + cooldown are still written."""
    home = tmp_path / "home"
    home.mkdir()
    # No .cc-channels at all.

    result = _run(home=home, label="worker-ramjac-1")

    assert result.returncode == 0
    assert not (home / ".cc-channels").exists()
    assert (home / ".treadmill" / "availability" / "worker-ramjac-1.json").exists()


def test_non_coordinator_dirs_in_cc_channels_ignored(tmp_path: Path) -> None:
    """Worker session dirs and other non-``coordinator-*`` siblings must
    not receive a broadcast — only the owning coordinator inbox."""
    home = tmp_path / "home"
    home.mkdir()
    owning_relay = _seed_coordinator_inbox(home, "ramjac")
    # Worker session dir alongside the coordinator dir.
    worker_relay = home / ".cc-channels" / "worker-ramjac-2" / "relay"
    worker_relay.mkdir(parents=True)

    result = _run(home=home, label="worker-ramjac-1")

    assert result.returncode == 0
    assert len(list(owning_relay.glob("*.md"))) == 1
    assert list(worker_relay.glob("*.md")) == []


# ── cooldown ─────────────────────────────────────────────────────────


def test_cooldown_blocks_second_broadcast(tmp_path: Path) -> None:
    """Two broadcasts within the 3600s window: the first writes files;
    the second exits without touching the coordinator inbox or the
    availability record."""
    home = tmp_path / "home"
    home.mkdir()
    relay = _seed_coordinator_inbox(home, "ramjac")

    first = _run(home=home, label="worker-ramjac-1")
    assert first.returncode == 0
    assert len(list(relay.glob("*.md"))) == 1

    avail = home / ".treadmill" / "availability" / "worker-ramjac-1.json"
    first_mtime = avail.stat().st_mtime

    time.sleep(0.05)
    second = _run(home=home, label="worker-ramjac-1")
    assert second.returncode == 0

    # No new relay file.
    assert len(list(relay.glob("*.md"))) == 1
    # Availability JSON not rewritten.
    assert avail.stat().st_mtime == first_mtime


def test_cooldown_expires_allows_rebroadcast(tmp_path: Path) -> None:
    """When the cooldown stamp is older than 3600s, the next broadcast
    fires again. Simulate elapsed time by writing a stale timestamp."""
    home = tmp_path / "home"
    home.mkdir()
    relay = _seed_coordinator_inbox(home, "ramjac")

    cooldown = (
        home / ".treadmill" / "session-state" / "worker-ramjac-1"
        / "last-idle-broadcast"
    )
    cooldown.parent.mkdir(parents=True)
    cooldown.write_text(f"{int(time.time()) - 4000}\n")

    result = _run(home=home, label="worker-ramjac-1")
    assert result.returncode == 0
    assert len(list(relay.glob("*.md"))) == 1
