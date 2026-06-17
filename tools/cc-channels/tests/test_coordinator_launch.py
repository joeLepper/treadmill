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
              echo "ANTHROPIC_MODEL=${{ANTHROPIC_MODEL:-}}"
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
        [str(LAUNCHER), "coordinator-ramjac"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert (home / ".treadmill" / "teams" / "ramjac").is_dir()


def test_coordinator_label_sources_coordinator_env(tmp_path: Path) -> None:
    """coordinator.env, when present, is sourced and its variables reach
    the spawned claude process — confirming the API-written file path
    survives the launcher → claude env handoff."""
    home = tmp_path / "home"
    home.mkdir()
    team_dir = home / ".treadmill" / "teams" / "ramjac"
    team_dir.mkdir(parents=True)
    (team_dir / "coordinator.env").write_text(
        "TREADMILL_ROLE=coordinator\nTREADMILL_COORDINATOR_PLANS=p-1,p-2\n"
    )
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "coordinator-ramjac"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert recorder.exists(), "fake claude did not run"
    recorded = recorder.read_text()
    assert "TREADMILL_ROLE=coordinator" in recorded
    assert "TREADMILL_COORDINATOR_PLANS=p-1,p-2" in recorded


def test_coordinator_workdir_pinned_to_per_label_subdir(tmp_path: Path) -> None:
    """ADR-0087 PR-H — coordinator cwd is the PER-LABEL subdir, not the
    team root. The rendered ``CLAUDE.md`` lives at
    ``<team>/<label>/CLAUDE.md``; pinning cwd to the subdir is what
    makes Claude Code's auto-discovery read it. argv[2] is still
    overridden (the per-label dir is canonical)."""
    home = tmp_path / "home"
    home.mkdir()
    misleading = tmp_path / "wrong-workdir"
    misleading.mkdir()
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "coordinator-ramjac", str(misleading)],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    recorded = recorder.read_text()
    expected_workdir = str(
        home / ".treadmill" / "teams" / "ramjac" / "coordinator-ramjac"
    )
    assert f"CWD: {expected_workdir}" in recorded


def test_coordinator_skips_dispatch_reminder(tmp_path: Path) -> None:
    """The 'reminder: dispatch with --created-by' line is for workers, not
    coordinators. Confirm it's suppressed in coordinator mode + a role-
    specific notice replaces it."""
    home = tmp_path / "home"
    home.mkdir()
    team_dir = home / ".treadmill" / "teams" / "ramjac"
    team_dir.mkdir(parents=True)
    (team_dir / "coordinator.env").write_text("TREADMILL_ROLE=coordinator\n")
    env, _ = _build_env(tmp_path, home=home)

    result = subprocess.run(
        [str(LAUNCHER), "coordinator-ramjac"],
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
    every dash-delimited segment. Per ADR-0087 PR-H, cwd is the per-label
    subdir under the team dir."""
    home = tmp_path / "home"
    home.mkdir()
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "coordinator-my-internal-repo"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    expected_team_dir = home / ".treadmill" / "teams" / "my-internal-repo"
    expected_session_dir = expected_team_dir / "coordinator-my-internal-repo"
    assert expected_session_dir.is_dir()
    recorded = recorder.read_text()
    assert f"CWD: {expected_session_dir}" in recorded


def test_coordinator_workdir_override_logs_notice(tmp_path: Path) -> None:
    """If the operator passed an explicit non-team workdir, the launcher
    overrides it and emits a stderr notice so the override is visible."""
    home = tmp_path / "home"
    home.mkdir()
    other = tmp_path / "operator-workdir"
    other.mkdir()
    env, _ = _build_env(tmp_path, home=home)

    result = subprocess.run(
        [str(LAUNCHER), "coordinator-ramjac", str(other)],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert "overriding workdir" in result.stderr
    assert str(other) in result.stderr


# ── ADR-0087 PR-H — per-label cwd for evaluator + worker roles ──────


def test_evaluator_label_pins_cwd_to_per_label_subdir(tmp_path: Path) -> None:
    """ADR-0087 PR-H — evaluator labels now get per-label workdir
    handling. cwd lands at ``<team>/<label>/`` so the evaluator's
    rendered CLAUDE.md is discovered."""
    home = tmp_path / "home"
    home.mkdir()
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "evaluator-ramjac"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    expected_session_dir = (
        home / ".treadmill" / "teams" / "ramjac" / "evaluator-ramjac"
    )
    assert expected_session_dir.is_dir()
    recorded = recorder.read_text()
    assert f"CWD: {expected_session_dir}" in recorded


def test_worker_team_label_pins_cwd_to_per_label_subdir(tmp_path: Path) -> None:
    """ADR-0087 PR-H — worker-<slug>-N labels now get per-label workdir
    handling. cwd lands at ``<team>/<label>/`` so the worker's rendered
    CLAUDE.md AND ``.claude/settings.json`` (which registers the
    PostToolUse relay-inject hook) are discovered."""
    home = tmp_path / "home"
    home.mkdir()
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "worker-ramjac-2"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    expected_session_dir = (
        home / ".treadmill" / "teams" / "ramjac" / "worker-ramjac-2"
    )
    assert expected_session_dir.is_dir()
    recorded = recorder.read_text()
    assert f"CWD: {expected_session_dir}" in recorded


def test_worker_repo_slug_with_dashes(tmp_path: Path) -> None:
    """``worker-<slug>-N`` parsing must handle slugs containing dashes.
    The regex ``^worker-(.+)-[0-9]+$`` captures the longest slug that
    still leaves a trailing numeric index, so ``worker-my-repo-1`` →
    slug ``my-repo`` + index ``1``, NOT slug ``my`` + index ``repo-1``."""
    home = tmp_path / "home"
    home.mkdir()
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "worker-my-internal-repo-3"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    expected_session_dir = (
        home / ".treadmill" / "teams" / "my-internal-repo"
        / "worker-my-internal-repo-3"
    )
    assert expected_session_dir.is_dir()
    recorded = recorder.read_text()
    assert f"CWD: {expected_session_dir}" in recorded


def test_orchestrator_label_does_not_trigger_team_role_handling(
    tmp_path: Path,
) -> None:
    """Orchestrator labels (``treadmill-alan``, ``treadmill-bert``, etc.)
    must NOT match any of the three role patterns. Verifies the per-
    label cwd handling stays scoped to team-role labels only."""
    home = tmp_path / "home"
    home.mkdir()
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "treadmill-alan"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    # No team dir created.
    assert not (home / ".treadmill" / "teams").exists()
    # CWD remained the launcher's invocation cwd (tmp_path).
    recorded = recorder.read_text()
    assert f"CWD: {tmp_path}" in recorded


def test_per_label_env_file_sourced_when_present(tmp_path: Path) -> None:
    """ADR-0087 PR-H — the per-label ``<label>/<label>.env`` file
    written by ``treadmill team up`` is sourced for every team-role
    session. Used to thread ``TREADMILL_ROLE`` / ``TREADMILL_LABEL`` /
    ``TREADMILL_API_URL`` into the spawned process."""
    home = tmp_path / "home"
    home.mkdir()
    session_dir = (
        home / ".treadmill" / "teams" / "ramjac" / "worker-ramjac-1"
    )
    session_dir.mkdir(parents=True)
    (session_dir / "worker-ramjac-1.env").write_text(
        "TREADMILL_ROLE=worker\nTREADMILL_API_URL=http://from-env\n"
    )
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "worker-ramjac-1"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    recorded = recorder.read_text()
    assert "TREADMILL_ROLE=worker" in recorded


def test_coordinator_env_sourced_before_cwd_change(tmp_path: Path) -> None:
    """``<team>/coordinator.env`` is sourced from the team root BEFORE
    the cwd changes to the per-label subdir. The env file's path
    predates per-label dirs and the API's plan-id write-through
    (ADR-0084 §3A) depends on it staying at <team>/coordinator.env."""
    home = tmp_path / "home"
    home.mkdir()
    team_dir = home / ".treadmill" / "teams" / "ramjac"
    team_dir.mkdir(parents=True)
    (team_dir / "coordinator.env").write_text(
        "TREADMILL_COORDINATOR_PLANS=plan-abc-123\n"
    )
    env, recorder = _build_env(tmp_path, home=home)

    subprocess.run(
        [str(LAUNCHER), "coordinator-ramjac"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    recorded = recorder.read_text()
    # The env value lands in claude's environment despite cwd having
    # moved to <team>/coordinator-ramjac/ before exec.
    assert "TREADMILL_COORDINATOR_PLANS=plan-abc-123" in recorded
    assert (
        f"CWD: {team_dir / 'coordinator-ramjac'}" in recorded
    )


# ── Per-role model pin (task 5d14fbcc — INCIDENT 2026-06-12) ────────


def test_orchestrator_label_carries_opus_model_fallback(tmp_path: Path) -> None:
    """task 5d14fbcc regression: orchestrator sessions (treadmill-alan
    etc.) carry no per-label .env, so they have no ANTHROPIC_MODEL unless
    the launcher provides a fallback. The launcher must export
    ANTHROPIC_MODEL=claude-opus-4-8 for orchestrators so --resume cannot
    fall back to the account-default (claude-fable-5, unavailable on this
    account — INCIDENT 2026-06-12: model-less sessions silently died)."""
    home = tmp_path / "home"
    home.mkdir()
    # Strip any host ANTHROPIC_MODEL so we test the launcher's own fallback.
    env, recorder = _build_env(tmp_path, home=home)
    env.pop("ANTHROPIC_MODEL", None)

    subprocess.run(
        [str(LAUNCHER), "treadmill-alan"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert recorder.exists(), "fake claude did not run"
    recorded = recorder.read_text()
    assert "ANTHROPIC_MODEL=claude-opus-4-8" in recorded


def test_team_role_env_model_not_overridden_by_launcher(tmp_path: Path) -> None:
    """When the per-label .env already carries ANTHROPIC_MODEL (written by
    treadmill team up), the launcher's fallback must not override it — a
    worker .env carries sonnet, not opus, and the ${var:-default} form must
    preserve it."""
    home = tmp_path / "home"
    home.mkdir()
    session_dir = (
        home / ".treadmill" / "teams" / "ramjac" / "worker-ramjac-1"
    )
    session_dir.mkdir(parents=True)
    (session_dir / "worker-ramjac-1.env").write_text(
        "TREADMILL_ROLE=worker\n"
        "TREADMILL_LABEL=worker-ramjac-1\n"
        "TREADMILL_API_URL=http://localhost:8088\n"
        "ANTHROPIC_MODEL=claude-sonnet-4-6\n"
    )
    env, recorder = _build_env(tmp_path, home=home)
    env.pop("ANTHROPIC_MODEL", None)

    subprocess.run(
        [str(LAUNCHER), "worker-ramjac-1"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=10,
    )

    assert recorder.exists(), "fake claude did not run"
    recorded = recorder.read_text()
    # Worker .env's sonnet pin must survive; opus fallback must not override.
    assert "ANTHROPIC_MODEL=claude-sonnet-4-6" in recorded
    assert "ANTHROPIC_MODEL=claude-opus-4-8" not in recorded
