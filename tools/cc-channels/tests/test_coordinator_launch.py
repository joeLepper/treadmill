"""Tests for the coordinator launch convention in launch-session.sh (ADR-0084 §3A).

The launcher's coordinator path:
  - detects label pattern ``coordinator-<repo-slug>``
  - creates ``~/.treadmill/teams/<repo-slug>/`` if absent
  - sources ``coordinator.env`` from that dir if present
  - pins WORKDIR to the team dir (notice on stderr if argv[2] differed)
  - skips the dispatch-reminder print

These tests stub out ``claude`` with a small bash script that records
its argv + environment to a file then exits 0, so the launcher reaches
``exec claude`` and we can assert on the recorded state without bringing
up an actual Claude session.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "launch-session.sh"


def _build_env(tmp_path: Path, *, home: Path) -> tuple[dict[str, str], Path]:
    """Return an environment dict suitable for running launch-session.sh
    under ``home``, with a fake ``claude`` on PATH that records its env
    + cwd to ``recorder.txt`` and exits 0. Returns (env, recorder_path)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = tmp_path / "recorder.txt"
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            {{
              echo "ARGS: $*"
              echo "CWD: $(pwd)"
              echo "TREADMILL_ROLE=${{TREADMILL_ROLE:-}}"
              echo "TREADMILL_COORDINATOR_PLANS=${{TREADMILL_COORDINATOR_PLANS:-}}"
              echo "TREADMILL_SESSION_LABEL=${{TREADMILL_SESSION_LABEL:-}}"
            }} > {recorder}
            """
        )
    )
    fake_claude.chmod(0o755)

    # Keep system bash + coreutils available; prepend our bin so claude
    # resolves to the stub.
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        # Suppress the real TREADMILL_RELAY_LEVEL etc. so coordinator.env
        # values aren't shadowed by host env.
        "TREADMILL_RELAY_LEVEL": "",
        "TREADMILL_API_URL": "",
    }
    # Strip TREADMILL_ROLE if it was set in the caller's environment so
    # the coordinator-mode detection is purely label-driven for these tests.
    env.pop("TREADMILL_ROLE", None)
    env.pop("TREADMILL_COORDINATOR_PLANS", None)
    return env, recorder


def test_coordinator_label_creates_team_dir(tmp_path: Path) -> None:
    """A `coordinator-<slug>` label creates ~/.treadmill/teams/<slug>/ even
    when no coordinator.env exists yet (the file is API-written at
    plan-start; the launcher must tolerate its absence)."""
    home = tmp_path / "home"
    home.mkdir()
    env, _ = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "coordinator-medicoder"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert (home / ".treadmill" / "teams" / "medicoder").is_dir()


def test_coordinator_label_sources_coordinator_env(tmp_path: Path) -> None:
    """coordinator.env, when present, is sourced and its variables reach
    the spawned claude process — confirming the API-written file path
    survives the launcher → claude env handoff."""
    home = tmp_path / "home"
    home.mkdir()
    team_dir = home / ".treadmill" / "teams" / "medicoder"
    team_dir.mkdir(parents=True)
    (team_dir / "coordinator.env").write_text(
        "TREADMILL_ROLE=coordinator\nTREADMILL_COORDINATOR_PLANS=p-1,p-2\n"
    )
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "coordinator-medicoder"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert recorder.exists(), "fake claude did not run"
    recorded = recorder.read_text()
    assert "TREADMILL_ROLE=coordinator" in recorded
    assert "TREADMILL_COORDINATOR_PLANS=p-1,p-2" in recorded


def test_coordinator_workdir_pinned_to_team_dir(tmp_path: Path) -> None:
    """The launcher pins cwd to the team dir regardless of what argv[2]
    supplied — coordinator workdir is canonical, not operator-overridable."""
    home = tmp_path / "home"
    home.mkdir()
    misleading = tmp_path / "wrong-workdir"
    misleading.mkdir()
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "coordinator-medicoder", str(misleading)],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    recorded = recorder.read_text()
    expected_workdir = str(home / ".treadmill" / "teams" / "medicoder")
    assert f"CWD: {expected_workdir}" in recorded


def test_coordinator_skips_dispatch_reminder(tmp_path: Path) -> None:
    """The 'reminder: dispatch with --created-by' line is for workers, not
    coordinators. Confirm it's suppressed in coordinator mode + a role-
    specific notice replaces it."""
    home = tmp_path / "home"
    home.mkdir()
    team_dir = home / ".treadmill" / "teams" / "medicoder"
    team_dir.mkdir(parents=True)
    (team_dir / "coordinator.env").write_text("TREADMILL_ROLE=coordinator\n")
    env, _ = _build_env(tmp_path, home=home)

    result = subprocess.run(
        [str(LAUNCHER), "coordinator-medicoder"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert "reminder: dispatch with --created-by" not in result.stderr
    assert "role=coordinator" in result.stderr


def test_worker_label_keeps_dispatch_reminder(tmp_path: Path) -> None:
    """Non-coordinator labels retain the existing dispatch-reminder print —
    we haven't broken the worker launch path."""
    home = tmp_path / "home"
    home.mkdir()
    env, _ = _build_env(tmp_path, home=home)

    result = subprocess.run(
        [str(LAUNCHER), "treadmill-bert"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert "reminder: dispatch with --created-by treadmill-bert" in result.stderr
    assert "role=coordinator" not in result.stderr


def test_worker_label_does_not_create_team_dir(tmp_path: Path) -> None:
    """A non-coordinator label must not create ~/.treadmill/teams/* — the
    team dir is exclusively a coordinator artifact."""
    home = tmp_path / "home"
    home.mkdir()
    env, _ = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "treadmill-bert"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert not (home / ".treadmill" / "teams").exists()


def test_coordinator_repo_slug_with_dashes(tmp_path: Path) -> None:
    """Repo slugs can contain dashes (e.g. `my-internal-repo`). The strip
    of the `coordinator-` prefix must remove only the literal prefix, not
    every dash-delimited segment."""
    home = tmp_path / "home"
    home.mkdir()
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "coordinator-my-internal-repo"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    expected_dir = home / ".treadmill" / "teams" / "my-internal-repo"
    assert expected_dir.is_dir()
    recorded = recorder.read_text()
    assert f"CWD: {expected_dir}" in recorded


def test_coordinator_workdir_override_logs_notice(tmp_path: Path) -> None:
    """If the operator passed an explicit non-team workdir, the launcher
    overrides it and emits a stderr notice so the override is visible."""
    home = tmp_path / "home"
    home.mkdir()
    other = tmp_path / "operator-workdir"
    other.mkdir()
    env, _ = _build_env(tmp_path, home=home)

    result = subprocess.run(
        [str(LAUNCHER), "coordinator-medicoder", str(other)],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert "overriding workdir" in result.stderr
    assert str(other) in result.stderr
