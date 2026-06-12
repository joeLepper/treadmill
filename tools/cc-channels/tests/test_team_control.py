"""Tests for treadmill-team-control (task 92365367, ADR-0091).

Real-process pattern (#326/#330/#333 convention): the genuine script
runs as a subprocess with ``systemctl`` and ``curl`` stubbed on PATH as
recorders, a sandboxed ``TREADMILL_TEAMS_ROOT``, and nothing about the
script's own logic mocked.

Coverage axes:
  * activate starts every team-label unit — and ONLY team-label units
    (orchestrator/operator units untouchable by construction);
  * pause is FAIL-CLOSED: refused when the decision API omits the team
    from quiescent_teams, when the API is unreachable (the sibling
    decision-API task may not even be deployed yet), and when the
    response is malformed — units are stopped in none of those cases;
  * pause proceeds (stop per unit) when the API affirmatively lists the
    team quiescent;
  * usage / slug-shape / missing-team errors;
  * the unit file declares SuccessExitStatus=143 (ADR-0091 §5) while
    keeping the #326 ExecStopPost reap.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

CONTROL = (
    Path(__file__).resolve().parents[1] / "systemd" / "treadmill-team-control"
)
UNIT = (
    Path(__file__).resolve().parents[1] / "systemd" / "treadmill-channel@.service"
)
SLUG = "teamx"


def _env(tmp_path: Path, *, decision: dict | None, curl_exit: int = 0) -> dict:
    """Sandbox: fake teams root with a full team, recorder stubs on PATH.

    ``decision=None`` with ``curl_exit!=0`` models an unreachable API;
    ``decision`` as a dict is served as the curl stdout.
    """
    teams = tmp_path / "teams" / SLUG
    for label in (
        f"coordinator-{SLUG}",
        f"evaluator-{SLUG}",
        f"worker-{SLUG}-1",
        f"worker-{SLUG}-2",
        "not-a-team-label",  # ignored by the label filter
    ):
        (teams / label).mkdir(parents=True, exist_ok=True)

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir(exist_ok=True)
    sysd_log = tmp_path / "systemctl.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{sysd_log}"\n')
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IEXEC)

    curl = fake_bin / "curl"
    if decision is not None:
        body = json.dumps(decision).replace("'", "'\\''")
        curl.write_text(f"#!/usr/bin/env bash\nprintf '%s' '{body}'\n")
    else:
        curl.write_text(f"#!/usr/bin/env bash\nexit {curl_exit}\n")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)

    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TREADMILL_TEAMS_ROOT": str(tmp_path / "teams"),
    }


def _run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(CONTROL), *args], env=env, capture_output=True, text=True, timeout=20,
    )


def _systemctl_lines(tmp_path: Path) -> list[str]:
    log = tmp_path / "systemctl.log"
    return log.read_text().splitlines() if log.exists() else []


# ── activate ─────────────────────────────────────────────────────────────────


def test_activate_starts_every_team_unit_and_nothing_else(tmp_path: Path) -> None:
    env = _env(tmp_path, decision=None, curl_exit=7)  # API state irrelevant

    result = _run(env, "activate", SLUG)

    assert result.returncode == 0, result.stderr
    lines = sorted(_systemctl_lines(tmp_path))
    assert lines == sorted(
        f"--user start treadmill-channel@{label}.service"
        for label in (
            f"coordinator-{SLUG}",
            f"evaluator-{SLUG}",
            f"worker-{SLUG}-1",
            f"worker-{SLUG}-2",
        )
    )
    # By-construction guarantee, asserted anyway: every touched unit is a
    # team-label unit of THIS slug; the non-team dir was ignored.
    for line in lines:
        assert f"treadmill-channel@" in line and SLUG in line
        assert "not-a-team-label" not in line


# ── pause: fail-closed ───────────────────────────────────────────────────────


def test_pause_refused_when_not_quiescent(tmp_path: Path) -> None:
    """THE safety property: a busy team is never paused."""
    env = _env(
        tmp_path,
        decision={"desired_team": "other", "quiescent_teams": ["other"], "reason": "x"},
    )

    result = _run(env, "pause", SLUG)

    assert result.returncode == 1
    assert "REFUSING to pause" in result.stderr
    assert "quiescent_teams" in result.stderr
    assert _systemctl_lines(tmp_path) == []  # nothing stopped


def test_pause_refused_when_api_unreachable(tmp_path: Path) -> None:
    """Fail-safe (plan 992d65b7): no decision → no pause. Also the
    deployment-ordering case — this script can ship before the sibling
    decision-API task, and must refuse rather than free-run."""
    env = _env(tmp_path, decision=None, curl_exit=7)

    result = _run(env, "pause", SLUG)

    assert result.returncode == 1
    assert "REFUSING to pause" in result.stderr
    assert "unavailable" in result.stderr
    assert _systemctl_lines(tmp_path) == []


def test_pause_refused_on_malformed_decision(tmp_path: Path) -> None:
    env = _env(tmp_path, decision=None, curl_exit=0)  # curl "succeeds", empty body

    result = _run(env, "pause", SLUG)

    assert result.returncode == 1
    assert "REFUSING to pause" in result.stderr
    assert _systemctl_lines(tmp_path) == []


def test_pause_stops_units_when_quiescent(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        decision={
            "desired_team": "other",
            "quiescent_teams": ["other", SLUG],
            "reason": "x",
        },
    )

    result = _run(env, "pause", SLUG)

    assert result.returncode == 0, result.stderr
    lines = sorted(_systemctl_lines(tmp_path))
    assert lines == sorted(
        f"--user stop treadmill-channel@{label}.service"
        for label in (
            f"coordinator-{SLUG}",
            f"evaluator-{SLUG}",
            f"worker-{SLUG}-1",
            f"worker-{SLUG}-2",
        )
    )


# ── argument / layout errors ─────────────────────────────────────────────────


def test_usage_errors(tmp_path: Path) -> None:
    env = _env(tmp_path, decision=None, curl_exit=7)
    assert _run(env, "activate").returncode == 2          # missing slug
    assert _run(env, "destroy", SLUG).returncode == 2     # unknown verb
    assert _run(env, "pause", "Bad/Slug").returncode == 2 # slug shape guard
    assert _systemctl_lines(tmp_path) == []


def test_missing_team_dir_refused(tmp_path: Path) -> None:
    env = _env(tmp_path, decision=None, curl_exit=7)
    result = _run(env, "activate", "ghost-team")
    assert result.returncode == 1
    assert "no team installed" in result.stderr
    assert _systemctl_lines(tmp_path) == []


# ── unit file (ADR-0091 §5) ──────────────────────────────────────────────────


def test_unit_declares_success_exit_status_and_keeps_reap() -> None:
    """An intentional stop (SIGTERM → exit 143) must not land the unit in
    'failed' (the 2026-06-12 papercut) — and the #326 ExecStopPost reap
    must survive the edit."""
    body = UNIT.read_text()
    assert "SuccessExitStatus=143" in body
    assert "treadmill-channel-reap %i" in body
    assert "Restart=on-failure" in body
