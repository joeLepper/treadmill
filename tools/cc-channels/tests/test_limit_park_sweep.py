"""Tests for treadmill-limit-park-sweep (task 2b8fd900).

The sweep is a durable, non-LLM systemd timer that loops over active
treadmill-channel@* units and bounces any confirmed parked at the Claude
usage-limit modal. It delegates detection to treadmill-limit-park-check
and event/failover logic to treadmill-limit-park-recover, then issues a
`systemctl --user restart` to recycle the frozen unit.

Harness: real-process pattern. The sweep is run as a subprocess with:
  - a temp script dir that holds stub `treadmill-limit-park-check` and
    `treadmill-limit-park-recover` alongside a copy of the sweep script
    (so SCRIPT_DIR resolves to the stubs);
  - PATH-injected stub `systemctl` that records calls and serves the
    list-units output;
  - no tmux, no API, no live sessions required.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

SYSTEMD_DIR = Path(__file__).resolve().parents[1] / "systemd"
SWEEP_SRC = SYSTEMD_DIR / "treadmill-limit-park-sweep"
SERVICE = SYSTEMD_DIR / "treadmill-limit-park-sweep.service"
TIMER = SYSTEMD_DIR / "treadmill-limit-park-sweep.timer"

ACTIVE_LABEL = "coordinator-sweepteam"
ACTIVE_UNIT = f"treadmill-channel@{ACTIVE_LABEL}.service"

# systemctl list-units output line for the one active label.
_LIST_LINE = f"{ACTIVE_UNIT}  active  running  Treadmill session\n"


def _make_sweep_dir(tmp_path: Path) -> Path:
    """Copy the sweep script into a temp dir so SCRIPT_DIR points there.

    Stub treadmill-limit-park-check and treadmill-limit-park-recover are
    placed alongside the copy; callers write the stub bodies.
    """
    sdir = tmp_path / "sweep_scripts"
    sdir.mkdir()
    sweep = sdir / "treadmill-limit-park-sweep"
    shutil.copy(SWEEP_SRC, sweep)
    sweep.chmod(sweep.stat().st_mode | stat.S_IEXEC)
    return sdir


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _make_systemctl(fake_bin: Path, systemctl_log: Path, list_output: str) -> None:
    """Stub systemctl: records every call; returns list_output for list-units."""
    body = (
        f'echo "$@" >> "{systemctl_log}"\n'
        "shift\n"  # drop --user
        'if [ "$1" = "list-units" ]; then\n'
        f"  printf '%s' '{list_output}'\n"
        "fi\n"
        "exit 0\n"
    )
    _write_stub(fake_bin / "systemctl", body)


def _run_sweep(sdir: Path, fake_bin: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(sdir / "treadmill-limit-park-sweep")],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=15,
    )


def _systemctl_calls(log: Path) -> list[str]:
    if not log.exists():
        return []
    return log.read_text().splitlines()


# ── Core recovery fixture ─────────────────────────────────────────────


def test_parked_modal_fixture_triggers_recovery(tmp_path: Path) -> None:
    """A parked-modal fixture (check exits 0) causes the sweep to call
    systemctl restart on the confirmed-parked unit."""
    sdir = _make_sweep_dir(tmp_path)
    # check: confirms park on first call (state already written by launcher loop)
    _write_stub(sdir / "treadmill-limit-park-check", "exit 0")
    # recover: escalate path (no fallback configured) — sweep still bounces
    _write_stub(sdir / "treadmill-limit-park-recover", "exit 2")

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    sysd_log = tmp_path / "systemctl.log"
    _make_systemctl(fake_bin, sysd_log, _LIST_LINE)

    result = _run_sweep(sdir, fake_bin)

    assert result.returncode == 0
    calls = _systemctl_calls(sysd_log)
    restart_calls = [c for c in calls if "restart" in c]
    assert len(restart_calls) == 1
    assert ACTIVE_UNIT in restart_calls[0]


def test_parked_modal_with_failover_triggers_recovery(tmp_path: Path) -> None:
    """Failover path (recover exits 0) also results in a systemctl restart."""
    sdir = _make_sweep_dir(tmp_path)
    _write_stub(sdir / "treadmill-limit-park-check", "exit 0")
    _write_stub(sdir / "treadmill-limit-park-recover", "exit 0")

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    sysd_log = tmp_path / "systemctl.log"
    _make_systemctl(fake_bin, sysd_log, _LIST_LINE)

    result = _run_sweep(sdir, fake_bin)

    assert result.returncode == 0
    restart_calls = [c for c in _systemctl_calls(sysd_log) if "restart" in c]
    assert any(ACTIVE_UNIT in c for c in restart_calls)


# ── No park: no restart ──────────────────────────────────────────────


def test_no_park_does_not_restart(tmp_path: Path) -> None:
    """When check returns 1 (not parked), the sweep issues no restart."""
    sdir = _make_sweep_dir(tmp_path)
    _write_stub(sdir / "treadmill-limit-park-check", "exit 1")
    _write_stub(sdir / "treadmill-limit-park-recover", "exit 0")

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    sysd_log = tmp_path / "systemctl.log"
    _make_systemctl(fake_bin, sysd_log, _LIST_LINE)

    result = _run_sweep(sdir, fake_bin)

    assert result.returncode == 0
    restart_calls = [c for c in _systemctl_calls(sysd_log) if "restart" in c]
    assert len(restart_calls) == 0


# ── No active units ───────────────────────────────────────────────────


def test_no_active_units_is_a_noop(tmp_path: Path) -> None:
    """When no treadmill-channel@* units are active, the sweep exits 0
    without calling check or restart."""
    sdir = _make_sweep_dir(tmp_path)
    _write_stub(sdir / "treadmill-limit-park-check", "exit 0")
    _write_stub(sdir / "treadmill-limit-park-recover", "exit 0")

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    sysd_log = tmp_path / "systemctl.log"
    _make_systemctl(fake_bin, sysd_log, "")  # empty list

    result = _run_sweep(sdir, fake_bin)

    assert result.returncode == 0
    restart_calls = [c for c in _systemctl_calls(sysd_log) if "restart" in c]
    assert len(restart_calls) == 0


# ── Multi-label: only parked units are bounced ────────────────────────


def test_only_parked_labels_are_bounced(tmp_path: Path) -> None:
    """With two active labels, only the one that confirms parked is bounced."""
    parked = "coordinator-alpha"
    healthy = "worker-alpha-1"
    list_output = (
        f"treadmill-channel@{parked}.service  active  running  Treadmill\n"
        f"treadmill-channel@{healthy}.service  active  running  Treadmill\n"
    )

    sdir = _make_sweep_dir(tmp_path)
    # check: parked iff label matches the parked label
    check_body = (
        f'if [ "$1" = "{parked}" ]; then exit 0; else exit 1; fi'
    )
    _write_stub(sdir / "treadmill-limit-park-check", check_body)
    _write_stub(sdir / "treadmill-limit-park-recover", "exit 2")

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    sysd_log = tmp_path / "systemctl.log"
    _make_systemctl(fake_bin, sysd_log, list_output)

    result = _run_sweep(sdir, fake_bin)

    assert result.returncode == 0
    restart_calls = [c for c in _systemctl_calls(sysd_log) if "restart" in c]
    assert len(restart_calls) == 1
    assert f"treadmill-channel@{parked}.service" in restart_calls[0]
    assert f"treadmill-channel@{healthy}.service" not in restart_calls[0]


# ── Restart failure doesn't abort the sweep ──────────────────────────


def test_restart_failure_does_not_abort_sweep(tmp_path: Path) -> None:
    """A failed systemctl restart on one label logs a warning but the
    sweep continues and exits 0."""
    sdir = _make_sweep_dir(tmp_path)
    _write_stub(sdir / "treadmill-limit-park-check", "exit 0")
    _write_stub(sdir / "treadmill-limit-park-recover", "exit 2")

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    sysd_log = tmp_path / "systemctl.log"
    # systemctl restart fails with exit 1
    body = (
        f'echo "$@" >> "{sysd_log}"\n'
        "shift\n"
        'if [ "$1" = "list-units" ]; then\n'
        f"  printf '%s' '{_LIST_LINE}'\n"
        "elif [ \"$1\" = \"restart\" ]; then\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    _write_stub(fake_bin / "systemctl", body)

    result = _run_sweep(sdir, fake_bin)

    assert result.returncode == 0
    assert "WARNING" in result.stderr


# ── Unit file pins ────────────────────────────────────────────────────


def test_service_is_oneshot() -> None:
    body = SERVICE.read_text()
    assert "Type=oneshot" in body


def test_timer_has_5h_calendar() -> None:
    body = TIMER.read_text()
    # OnCalendar=*-*-* 00/5:00:00 fires at 00:00, 05:00, 10:00, 15:00, 20:00
    assert "OnCalendar=*-*-* 00/5:00:00" in body


def test_timer_is_persistent() -> None:
    body = TIMER.read_text()
    assert "Persistent=true" in body


def test_service_names_sweep_script() -> None:
    body = SERVICE.read_text()
    assert "treadmill-limit-park-sweep" in body
