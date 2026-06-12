"""Tests for treadmill-channel-reap (task 969fe369 — the orphan crashloop).

The 2026-06-11 failure class: a LIVE launcher survives `systemctl stop`
detached from the unit cgroup (tmux re-parents the tree into a
tmux-spawn scope), keeps holding the ADR-0073 lock, and the unit
crashloops against its own orphan. The reap script is the unit's
ExecStopPost: tmux session down, lock-holder reaped, pidfile gone —
always exit 0.

Harness: same pattern as test_launcher_singleton.py — fake HOME, real
processes. Orphans are bash scripts NAMED ``claude`` (the pidfile PID is
the launcher post-``exec claude``, so its cmdline names claude — the
script's PID-reuse identity guard keys on that). tmux is stubbed on
PATH with a recorder so no real tmux server is touched.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

REAP = (
    Path(__file__).resolve().parents[1] / "systemd" / "treadmill-channel-reap"
)
UNIT = (
    Path(__file__).resolve().parents[1] / "systemd" / "treadmill-channel@.service"
)
LABEL = "reap-test-label"


def _env(tmp_path: Path) -> dict[str, str]:
    """Fake HOME + stubbed tmux that records its argv lines."""
    home = tmp_path / "home"
    (home / ".cc-channels" / LABEL).mkdir(parents=True, exist_ok=True)
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir(exist_ok=True)
    tmux_log = tmp_path / "tmux.log"
    tmux = fake_bin / "tmux"
    tmux.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{tmux_log}"\n')
    tmux.chmod(tmux.stat().st_mode | stat.S_IEXEC)
    return {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }


def _pidfile(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".cc-channels" / LABEL / "launcher.pid"


def _tmux_log(tmp_path: Path) -> str:
    log = tmp_path / "tmux.log"
    return log.read_text() if log.exists() else ""


def _spawn_orphan(
    tmp_path: Path,
    *,
    trap_term: bool = False,
    label: str | None = LABEL,
) -> subprocess.Popen:
    """A live process whose cmdline contains ``claude`` (like the real
    lock-holder, which is the launcher post-exec).

    ``label`` lands in the orphan's environment as
    ``TREADMILL_SESSION_LABEL`` — the launcher exports it before exec'ing
    claude, and the reap script's PID-recycling identity guard
    (task f9cb6ce3) reads it back from ``/proc/<pid>/environ``. ``None``
    spawns a holder with no label at all (a recycled PID that happens to
    run something claude-named but is not a session launcher).
    """
    script = tmp_path / "claude"
    body = "#!/usr/bin/env bash\n"
    if trap_term:
        body += "trap '' TERM\n"
    body += "sleep 60\n"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    env = {k: v for k, v in os.environ.items() if k != "TREADMILL_SESSION_LABEL"}
    if label is not None:
        env["TREADMILL_SESSION_LABEL"] = label
    return subprocess.Popen([str(script)], env=env)


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(REAP), LABEL], env=env, capture_output=True, text=True, timeout=15,
    )


def test_live_orphan_is_reaped_and_lock_cleared(tmp_path: Path) -> None:
    """THE incident shape: live lock-holder + tmux session → both gone,
    exit 0."""
    env = _env(tmp_path)
    orphan = _spawn_orphan(tmp_path)
    try:
        _pidfile(env).write_text(str(orphan.pid))

        result = _run(env)

        assert result.returncode == 0, result.stderr
        # TERM landed: the orphan is gone (poll briefly — TERM is async).
        for _ in range(20):
            if orphan.poll() is not None:
                break
            time.sleep(0.1)
        assert orphan.poll() is not None, "orphan still alive after reap"
        assert not _pidfile(env).exists()
        assert f"kill-session -t {LABEL}" in _tmux_log(tmp_path)
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait()


def test_term_resistant_orphan_gets_killed(tmp_path: Path) -> None:
    """An orphan ignoring TERM is KILLed after the grace window."""
    env = _env(tmp_path)
    orphan = _spawn_orphan(tmp_path, trap_term=True)
    try:
        _pidfile(env).write_text(str(orphan.pid))

        result = _run(env)

        assert result.returncode == 0, result.stderr
        for _ in range(20):
            if orphan.poll() is not None:
                break
            time.sleep(0.1)
        assert orphan.poll() is not None, "TERM-resistant orphan survived"
        assert not _pidfile(env).exists()
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait()


def test_missing_pidfile_is_noop_success(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    # tmux teardown still attempted (a session can outlive its pidfile).
    assert f"kill-session -t {LABEL}" in _tmux_log(tmp_path)


def test_dead_pid_clears_lock_without_killing(tmp_path: Path) -> None:
    """REGRESSION (per the task spec): the dead-PID stale-lock case still
    auto-clears — a power-cut leftover must not need manual recovery."""
    env = _env(tmp_path)
    sleeper = subprocess.Popen(["sleep", "1"])
    sleeper.wait()  # now certainly dead
    _pidfile(env).write_text(str(sleeper.pid))

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert not _pidfile(env).exists()


def test_garbage_pidfile_clears_without_error(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _pidfile(env).write_text("not-a-pid\n")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert not _pidfile(env).exists()


def test_pid_reuse_innocent_process_not_killed(tmp_path: Path) -> None:
    """A pidfile pointing at a live process whose cmdline is NOT a
    launcher/claude (PID reuse) drops the stale lock but leaves the
    process alive."""
    env = _env(tmp_path)
    innocent = subprocess.Popen(["sleep", "60"])
    try:
        _pidfile(env).write_text(str(innocent.pid))

        result = _run(env)

        assert result.returncode == 0, result.stderr
        assert innocent.poll() is None, "innocent process was killed"
        assert not _pidfile(env).exists()
        assert "not a launcher" in result.stderr
    finally:
        innocent.kill()
        innocent.wait()


def test_missing_label_is_noop_success(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(REAP)], env=_env(tmp_path), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0


def test_unit_template_wires_execstoppost() -> None:
    """The unit template carries the reap as ExecStopPost — the
    load-bearing line (KillMode can't reach the tmux-parented tree)."""
    body = UNIT.read_text()
    assert "ExecStopPost=" in body
    assert "treadmill-channel-reap %i" in body
    # Still restart-on-failure: ExecStopPost must run between failed
    # attempts for the self-healing property.
    assert "Restart=on-failure" in body


# ── PID-recycling identity guard + gated pidfile rm (task f9cb6ce3) ──────────


def test_recycled_pid_other_label_not_killed(tmp_path: Path) -> None:
    """THE f9cb6ce3 case: the pidfile PID was recycled onto a SIBLING
    session's claude process — cmdline passes the old guard, but the
    environ label names another session. Must not kill it; the stale
    lock still clears."""
    env = _env(tmp_path)
    sibling = _spawn_orphan(tmp_path, label="some-other-label")
    try:
        _pidfile(env).write_text(str(sibling.pid))

        result = _run(env)

        assert result.returncode == 0, result.stderr
        time.sleep(0.3)  # give a wrong kill time to land before asserting
        assert sibling.poll() is None, "sibling session's process was killed"
        assert not _pidfile(env).exists()
        assert "label mismatch" in result.stderr
        assert "some-other-label" in result.stderr
    finally:
        sibling.kill()
        sibling.wait()


def test_claude_named_process_without_label_not_killed(tmp_path: Path) -> None:
    """A claude-named process with NO session label in its environment is
    not a launcher-descendant — recycled PID, leave alive, clear lock."""
    env = _env(tmp_path)
    impostor = _spawn_orphan(tmp_path, label=None)
    try:
        _pidfile(env).write_text(str(impostor.pid))

        result = _run(env)

        assert result.returncode == 0, result.stderr
        time.sleep(0.3)
        assert impostor.poll() is None, "label-less process was killed"
        assert not _pidfile(env).exists()
        assert "label mismatch" in result.stderr
    finally:
        impostor.kill()
        impostor.wait()


def test_kill_surviving_holder_keeps_pidfile(tmp_path: Path) -> None:
    """The D-state edge (f9cb6ce3 secondary): a holder that survives
    KILL must KEEP the pidfile so the ADR-0073 singleton guard blocks a
    double instance on the next start.

    ``kill`` is a bash BUILTIN, so a PATH stub can't intercept it; we
    inject a no-op ``kill`` shell FUNCTION via BASH_ENV (sourced by
    non-interactive bash), which makes every liveness probe report
    "alive" and every signal a no-op — exactly how an unkillable D-state
    holder looks to the script."""
    env = _env(tmp_path)
    holder = _spawn_orphan(tmp_path)  # matching label; never actually signalled
    try:
        _pidfile(env).write_text(str(holder.pid))
        bash_env = tmp_path / "bash_env.sh"
        bash_env.write_text("kill() { return 0; }\n")
        env = {**env, "BASH_ENV": str(bash_env)}

        result = _run(env)

        assert result.returncode == 0, result.stderr
        assert _pidfile(env).exists(), "pidfile removed despite live holder"
        assert "survived KILL" in result.stderr
        assert holder.poll() is None  # the stubbed kill really sent nothing
    finally:
        holder.kill()
        holder.wait()


def test_reaped_holder_carries_matching_label(tmp_path: Path) -> None:
    """Positive control for the identity guard: the happy-path orphan in
    the incident-shape tests carries OUR label and IS reaped — proven
    here explicitly so the guard tests can't all pass via a guard that
    simply never kills."""
    env = _env(tmp_path)
    orphan = _spawn_orphan(tmp_path, label=LABEL)
    try:
        _pidfile(env).write_text(str(orphan.pid))

        result = _run(env)

        assert result.returncode == 0, result.stderr
        for _ in range(20):
            if orphan.poll() is not None:
                break
            time.sleep(0.1)
        assert orphan.poll() is not None, "matching-label holder not reaped"
        assert not _pidfile(env).exists()
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait()
