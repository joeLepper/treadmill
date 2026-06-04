"""Tests for the launch-session.sh single-instance guard (ADR-0073).

The launcher writes its own PID to ``~/.cc-channels/<label>/launcher.pid``
right before ``exec claude``; a second invocation for the same label must
detect the live PID and refuse to start. The shell exits non-zero BEFORE
reaching ``exec claude``, so the test does not need a Claude stub — we
seed a pidfile pointing at a long-lived ``sleep`` child and check that
``launch-session.sh`` refuses on stderr with a non-zero exit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "launch-session.sh"


def test_launcher_refuses_when_live_pid_in_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    label = "test-label"
    state_dir = home / ".cc-channels" / label
    state_dir.mkdir(parents=True)
    pidfile = state_dir / "launcher.pid"

    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        pidfile.write_text(str(sleeper.pid))
        result = subprocess.run(
            [str(LAUNCHER), label],
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            timeout=10,
        )
    finally:
        sleeper.terminate()
        sleeper.wait()

    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "already alive" in result.stderr, (
        f"expected 'already alive' refusal on stderr, got: {result.stderr!r}"
    )
    # The pidfile we seeded must NOT have been removed: the live-PID branch
    # exits without touching it. Stale-cleanup is a different code path.
    assert pidfile.read_text().strip() == str(sleeper.pid)
