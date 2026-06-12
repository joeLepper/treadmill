"""Tests for the deploy-watcher single-instance lock (task f82f7590).

The 2026-06-11 dual-watcher outage class: the pidfile lived under the
CWD-RELATIVE ``.treadmill-local/`` state dir, so two worktrees each
running ``up`` got two private pidfiles and two "legitimate" watchers
racing to recreate the ONE shared treadmill-api container. The fix is a
host-global ``flock`` keyed by deployment id, acquired by the watcher
itself — kernel-released on death, so no TOCTOU and no PID-recycling
class (the #330 reap-guard lesson, one level up).

Harness: real processes per the #326/#330 convention — lock holders are
genuine child interpreters acquiring the genuine lock; nothing about
flock is mocked. ``acquire_watcher_lock`` takes an explicit path, so the
locks live under pytest's tmp dirs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from treadmill_local.deploy_watcher import (
    _LockReleasingGuard,
    acquire_watcher_lock,
    watcher_lock_path,
    watcher_pid_path,
)
from treadmill_local.runtime import LocalRuntime

PKG_DIR = Path(__file__).resolve().parents[1]


# ── Path conventions ─────────────────────────────────────────────────────────


def test_lock_and_pid_paths_are_host_global(monkeypatch) -> None:
    """The lock/pid paths key on HOME + deployment id — NEVER on the cwd.
    Per-worktree (cwd-relative) scoping is exactly the dual-watcher bug."""
    monkeypatch.setenv("HOME", "/fake-home")
    lock = watcher_lock_path("dep-1")
    pid = watcher_pid_path("dep-1")
    assert lock == Path("/fake-home/.treadmill/deploy-watcher.dep-1.lock")
    assert pid == Path("/fake-home/.treadmill/deploy-watcher.dep-1.pid")
    # Changing cwd must not move the lock.
    monkeypatch.chdir("/tmp")
    assert watcher_lock_path("dep-1") == lock


# ── Lock semantics (real processes) ──────────────────────────────────────────


def _spawn_holder(lock_path: Path) -> subprocess.Popen:
    """A real child interpreter that acquires the REAL lock and holds it."""
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(PKG_DIR)!r})
        from pathlib import Path
        from treadmill_local.deploy_watcher import acquire_watcher_lock
        fd = acquire_watcher_lock(Path({str(lock_path)!r}))
        if fd is None:
            print("refused", flush=True)
            sys.exit(3)
        print("acquired", flush=True)
        import time
        time.sleep(60)
        """
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )


def _wait_line(proc: subprocess.Popen) -> str:
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line, "holder child produced no status line"
    return line


def test_second_watcher_refused_while_holder_lives(tmp_path: Path) -> None:
    """THE dual-watcher shape: worktree A's watcher holds the lock;
    worktree B's attempt must be refused — exactly one watcher per
    deployment no matter how many `up`s run."""
    lock_path = tmp_path / "deploy-watcher.dep.lock"
    holder = _spawn_holder(lock_path)
    try:
        assert _wait_line(holder) == "acquired"

        fd = acquire_watcher_lock(lock_path)

        assert fd is None, "second watcher acquired a held lock"
        # Diagnostics name the winner so the loser's log is actionable.
        content = lock_path.read_text()
        assert f"pid={holder.pid}" in content
    finally:
        holder.kill()
        holder.wait()


def test_lock_released_on_holder_death(tmp_path: Path) -> None:
    """flock is kernel-released on process death: a successor acquires
    with no manual cleanup — no stale-lock recovery procedure exists or
    is needed (contrast: the ADR-0073 pidfile needed the #326 reap)."""
    lock_path = tmp_path / "deploy-watcher.dep.lock"
    holder = _spawn_holder(lock_path)
    assert _wait_line(holder) == "acquired"
    holder.kill()
    holder.wait()

    fd = acquire_watcher_lock(lock_path)

    assert fd is not None, "lock not released by holder death"
    assert f"pid={os.getpid()}" in lock_path.read_text()
    os.close(fd)


def test_dual_watcher_race_run_log(tmp_path: Path, capsys) -> None:
    """Run-log evidence for the PR: A acquires; B refuses while A lives;
    A dies; C acquires. The full lifecycle of the exactly-one invariant,
    against real processes and a real flock."""
    lock_path = tmp_path / "deploy-watcher.dep.lock"

    a = _spawn_holder(lock_path)
    assert _wait_line(a) == "acquired"
    print(f"RUNLOG: watcher A (pid={a.pid}) acquired {lock_path.name}")

    b = _spawn_holder(lock_path)
    assert _wait_line(b) == "refused"
    assert b.wait(timeout=10) == 3
    print(f"RUNLOG: watcher B (pid={b.pid}) REFUSED while A lives — exit 3")

    a.kill()
    a.wait()
    print(f"RUNLOG: watcher A (pid={a.pid}) killed")

    c = _spawn_holder(lock_path)
    assert _wait_line(c) == "acquired"
    print(f"RUNLOG: watcher C (pid={c.pid}) acquired after A's death")
    c.kill()
    c.wait()


def test_same_process_reopen_also_conflicts(tmp_path: Path) -> None:
    """flock conflicts across open file descriptions even within one
    process — the reason the staleness re-exec must RELEASE before
    ``os.execv`` (the fresh main() re-opens and would deadlock against
    its own inherited fd)."""
    lock_path = tmp_path / "deploy-watcher.dep.lock"
    fd = acquire_watcher_lock(lock_path)
    assert fd is not None
    try:
        assert acquire_watcher_lock(lock_path) is None
    finally:
        os.close(fd)


def test_lock_releasing_guard_frees_lock_before_reexec(tmp_path: Path) -> None:
    """The re-exec wrapper closes the lock fd BEFORE delegating to the
    real guard's reexec, so the fresh main() can re-acquire."""

    class _StubGuard:
        def __init__(self) -> None:
            self.reexec_called_with: list[Path | None] = []

        def changed(self) -> bool:
            return True

        def reexec(self, pid_file: Path | None = None) -> None:
            self.reexec_called_with.append(pid_file)

    lock_path = tmp_path / "deploy-watcher.dep.lock"
    fd = acquire_watcher_lock(lock_path)
    assert fd is not None
    stub = _StubGuard()
    wrapper = _LockReleasingGuard(stub, fd)
    assert wrapper.changed() is True

    wrapper.reexec(tmp_path / "w.pid")

    assert stub.reexec_called_with == [tmp_path / "w.pid"]
    # The lock is free again: a fresh acquire (what the re-exec'd
    # main() does) succeeds.
    fd2 = acquire_watcher_lock(lock_path)
    assert fd2 is not None
    os.close(fd2)


# ── Parent-side identity guard (#330 lesson) ─────────────────────────────────


def test_watcher_identity_ok_rejects_unrelated_process() -> None:
    """A recycled PID pointing at a non-watcher process must read as
    NOT-a-watcher (stale pidfile), never as a stop/trust target."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    try:
        assert LocalRuntime._watcher_identity_ok(proc.pid) is False
    finally:
        proc.kill()
        proc.wait()


def test_watcher_identity_ok_accepts_watcher_cmdline() -> None:
    """A process whose cmdline names the deploy_watcher module passes."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "# treadmill_local.deploy_watcher stand-in\n"
            "import time; time.sleep(60)",
        ],
    )
    try:
        # /proc/<pid>/cmdline carries the -c source text — but only once
        # the child's exec completes; poll briefly to avoid racing it.
        for _ in range(50):
            if LocalRuntime._watcher_identity_ok(proc.pid):
                break
            time.sleep(0.05)
        assert LocalRuntime._watcher_identity_ok(proc.pid) is True
    finally:
        proc.kill()
        proc.wait()


def test_watcher_identity_ok_dead_pid_is_false() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    # PID is dead (may linger as zombie until wait() — already reaped here).
    assert LocalRuntime._watcher_identity_ok(proc.pid) is False
