"""Tests for the broadcast-idle Stop hook (ADR-0084).

Pins the skip conditions + the broadcast shape:
- TREADMILL_SESSION_LABEL unset → no-op
- coordinator-* label → no-op
- cooldown active (< 300s since last broadcast) → no-op
- first broadcast → writes availability JSON + a relay file in every
  ``~/.cc-channels/coordinator-*/relay/`` dir found
- second broadcast within 300s → skipped (cooldown)

The hook is invoked as a subprocess with a synthetic ``HOME`` so
filesystem effects are isolated to ``tmp_path``.
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


def _seed_coordinator_dirs(home: Path, slugs: list[str]) -> list[Path]:
    """Create ``~/.cc-channels/coordinator-<slug>/relay/`` for each slug.
    Returns the relay paths."""
    relays: list[Path] = []
    for slug in slugs:
        relay = home / ".cc-channels" / f"coordinator-{slug}" / "relay"
        relay.mkdir(parents=True)
        relays.append(relay)
    return relays


def test_unset_label_is_noop(tmp_path: Path) -> None:
    """Without ``TREADMILL_SESSION_LABEL``, the hook returns 0 and
    writes nothing — Stop hooks fire on every session, but only labeled
    worker sessions broadcast."""
    home = tmp_path / "home"
    home.mkdir()
    _seed_coordinator_dirs(home, ["medicoder"])

    result = _run(home=home, label=None)

    assert result.returncode == 0
    relay_files = list(
        (home / ".cc-channels" / "coordinator-medicoder" / "relay").glob("*.md")
    )
    assert relay_files == []
    assert not (home / ".treadmill" / "availability").exists()


def test_coordinator_label_is_noop(tmp_path: Path) -> None:
    """A label starting with ``coordinator-`` short-circuits — coordinators
    don't broadcast availability to other coordinators."""
    home = tmp_path / "home"
    home.mkdir()
    _seed_coordinator_dirs(home, ["medicoder"])

    result = _run(home=home, label="coordinator-medicoder")

    assert result.returncode == 0
    relay_files = list(
        (home / ".cc-channels" / "coordinator-medicoder" / "relay").glob("*.md")
    )
    assert relay_files == []


def test_first_broadcast_writes_files(tmp_path: Path) -> None:
    """On a fresh state, the hook writes the availability JSON + drops a
    relay file into each coordinator inbox + records a cooldown stamp."""
    home = tmp_path / "home"
    home.mkdir()
    relays = _seed_coordinator_dirs(home, ["medicoder", "treadmill"])

    result = _run(home=home, label="treadmill-bert")

    assert result.returncode == 0

    # Availability JSON written
    avail = home / ".treadmill" / "availability" / "treadmill-bert.json"
    assert avail.exists()
    record = json.loads(avail.read_text())
    assert record["label"] == "treadmill-bert"
    assert isinstance(record["available_since"], int)
    assert isinstance(record["updated_at"], int)

    # Cooldown stamp written
    cooldown = (
        home / ".treadmill" / "session-state" / "treadmill-bert"
        / "last-idle-broadcast"
    )
    assert cooldown.exists()

    # Each coordinator inbox has exactly one relay file with the right shape
    for relay_dir in relays:
        files = list(relay_dir.glob("*.md"))
        assert len(files) == 1
        body = files[0].read_text()
        assert body.startswith("[AVAILABLE]\n\n")
        assert "[from: treadmill-bert]" in body
        assert "Worker treadmill-bert is idle" in body
        # Filename convention: <ns_ts>-<token>-available-from-<label>.md
        assert files[0].name.endswith("-available-from-treadmill-bert.md")


def test_cooldown_blocks_second_broadcast(tmp_path: Path) -> None:
    """Two broadcasts within the 300s window: the first writes files;
    the second exits without touching either coordinator inbox or the
    availability record."""
    home = tmp_path / "home"
    home.mkdir()
    relays = _seed_coordinator_dirs(home, ["medicoder"])

    first = _run(home=home, label="treadmill-bert")
    assert first.returncode == 0
    first_count = len(list(relays[0].glob("*.md")))
    assert first_count == 1

    # Capture the availability mtime to confirm it isn't rewritten
    avail = home / ".treadmill" / "availability" / "treadmill-bert.json"
    first_mtime = avail.stat().st_mtime

    # Sleep briefly so any second write would be observable, then re-run.
    time.sleep(0.05)
    second = _run(home=home, label="treadmill-bert")
    assert second.returncode == 0

    # No new relay file written
    assert len(list(relays[0].glob("*.md"))) == first_count
    # Availability JSON not rewritten
    assert avail.stat().st_mtime == first_mtime


def test_cooldown_expires_allows_rebroadcast(tmp_path: Path) -> None:
    """When the cooldown stamp is older than 300s, the next broadcast
    fires again. Simulate elapsed time by writing a stale timestamp."""
    home = tmp_path / "home"
    home.mkdir()
    relays = _seed_coordinator_dirs(home, ["medicoder"])

    # Seed a stale cooldown stamp (400s ago) directly on disk
    cooldown = (
        home / ".treadmill" / "session-state" / "treadmill-bert"
        / "last-idle-broadcast"
    )
    cooldown.parent.mkdir(parents=True)
    cooldown.write_text(f"{int(time.time()) - 400}\n")

    result = _run(home=home, label="treadmill-bert")
    assert result.returncode == 0

    files = list(relays[0].glob("*.md"))
    assert len(files) == 1


def test_no_coordinators_no_error(tmp_path: Path) -> None:
    """When ``~/.cc-channels/`` exists but has no ``coordinator-*`` dirs,
    the hook still records availability + cooldown but writes no relay
    files. No error."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".cc-channels").mkdir()
    (home / ".cc-channels" / "treadmill-alice" / "relay").mkdir(parents=True)

    result = _run(home=home, label="treadmill-alice")

    assert result.returncode == 0
    # No coordinator inbox got a relay file
    other_relay_files = list(
        (home / ".cc-channels" / "treadmill-alice" / "relay").glob("*.md")
    )
    assert other_relay_files == []
    # Availability still recorded
    avail = home / ".treadmill" / "availability" / "treadmill-alice.json"
    assert avail.exists()


def test_channels_root_missing_is_noop_no_error(tmp_path: Path) -> None:
    """If ``~/.cc-channels`` does not exist at all (fresh install), the
    hook still records availability for this worker but writes no relay
    files. Exits 0."""
    home = tmp_path / "home"
    home.mkdir()
    # No .cc-channels at all

    result = _run(home=home, label="treadmill-bert")

    assert result.returncode == 0
    avail = home / ".treadmill" / "availability" / "treadmill-bert.json"
    assert avail.exists()


def test_multi_coordinator_broadcast_each_inbox(tmp_path: Path) -> None:
    """Three coordinator inboxes → three relay files, one per inbox."""
    home = tmp_path / "home"
    home.mkdir()
    relays = _seed_coordinator_dirs(
        home, ["medicoder", "treadmill", "ramjac"]
    )

    result = _run(home=home, label="treadmill-bert")
    assert result.returncode == 0

    for relay_dir in relays:
        files = list(relay_dir.glob("*.md"))
        assert len(files) == 1, f"coordinator {relay_dir} missing file"


def test_other_dirs_in_cc_channels_ignored(tmp_path: Path) -> None:
    """Worker session dirs (``~/.cc-channels/treadmill-*``) and other
    non-``coordinator-*`` siblings must not receive a broadcast — the
    glob is exact-prefix on ``coordinator-``."""
    home = tmp_path / "home"
    home.mkdir()
    relays = _seed_coordinator_dirs(home, ["medicoder"])
    # Worker session dir alongside the coordinator dir
    worker_relay = home / ".cc-channels" / "treadmill-donna" / "relay"
    worker_relay.mkdir(parents=True)

    result = _run(home=home, label="treadmill-bert")
    assert result.returncode == 0

    assert len(list(relays[0].glob("*.md"))) == 1
    assert list(worker_relay.glob("*.md")) == []
