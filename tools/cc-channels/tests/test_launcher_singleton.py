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
CHANNEL_LAUNCH = Path(__file__).resolve().parents[1] / "systemd" / "treadmill-channel-launch"


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


def test_launch_wrapper_fails_loud_when_tmux_missing(tmp_path: Path) -> None:
    """treadmill-channel-launch must exit non-zero with a clear error when tmux is absent."""
    # Build a PATH that contains bash + env (needed to interpret the script)
    # but not tmux. Pointing PATH at /dev/null would also hide bash and the
    # shebang would fail with a confusing exit 126 before reaching our check.
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    import shutil

    for tool in ("bash", "env"):
        src = shutil.which(tool)
        if src is None:
            import pytest

            pytest.skip(f"{tool} not on host PATH; cannot construct test env")
        os.symlink(src, fake_bin / tool)

    result = subprocess.run(
        [str(CHANNEL_LAUNCH), "test-label"],
        env={**os.environ, "PATH": str(fake_bin)},
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=10,
    )
    assert result.returncode != 0, (
        f"expected non-zero exit when tmux missing, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    assert "tmux not installed" in result.stderr, (
        f"expected 'tmux not installed' on stderr, got: {result.stderr!r}"
    )
    install_hints = ["apt install", "pacman -S", "brew install"]
    assert any(hint in result.stderr for hint in install_hints), (
        f"expected at least one install hint on stderr, got: {result.stderr!r}"
    )
