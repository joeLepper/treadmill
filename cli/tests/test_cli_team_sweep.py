"""CLI foils for ``treadmill team sweep`` (ADR-0109 idle-sweep).

The sweep is AUTONOMOUS teardown — no human reads the drain lists before it acts —
so the dangerous direction is a FALSE tear-down. Each foil pins a SKIP case (the
team must be left standing) plus the one positive case (ephemeral + clean +
idle>grace → torn down). The sweep COMPOSES the drain-guard endpoint; it never
reimplements the in-flight check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from treadmill_cli.api_client import ApiError
from treadmill_cli.commands import team as team_module
from treadmill_cli.commands.team import team_app
from typer.testing import CliRunner

runner = CliRunner()

_NOW = datetime.now(UTC)
_OLD = (_NOW - timedelta(hours=48)).isoformat()  # well past any grace
_RECENT = (_NOW - timedelta(minutes=5)).isoformat()  # within grace


def _cfg(repo: str, lifecycle: str = "ephemeral") -> dict:
    slug = repo.replace("/", "-").lower()
    return {
        "repo": repo,
        "coordinator_label": f"coordinator-{slug}",
        "evaluator_label": f"evaluator-{slug}",
        "worker_labels": [f"worker-{slug}-1", f"worker-{slug}-2", f"worker-{slug}-3"],
        "lifecycle": lifecycle,
        "merge_target": "feature-branch",
    }


def _drain(clean: bool, last_activity_at: str | None) -> dict:
    return {
        "repo": "x",
        "clean": clean,
        "blocking": []
        if clean
        else [{"task_id": "t1", "derived_status": "wf: executing", "reason": "team_active"}],
        "parked": [],
        "last_activity_at": last_activity_at,
    }


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch):
    """Route _request by (method, path). Tests set `.configs` and `.drains`."""
    fake = MagicMock()
    state: dict = {"configs": [], "drains": {}}

    def _request(method: str, path: str, **_):
        if path == "/api/v1/team_configs":
            return state["configs"]
        if path.endswith("/drain"):
            repo = path[len("/api/v1/team_configs/") : -len("/drain")]
            val = state["drains"][repo]
            if isinstance(val, Exception):
                raise val  # simulate a drain-endpoint failure for this repo
            return val
        raise AssertionError(f"unexpected request {method} {path}")

    fake._request.side_effect = _request
    fake._state = state

    class _Factory:
        def __init__(self, _config):
            pass

        def __enter__(self_inner):
            return fake

        def __exit__(self_inner, *args):
            return False

    monkeypatch.setattr(team_module, "ApiClient", _Factory)
    monkeypatch.setattr(
        team_module, "load_config", lambda: MagicMock(api_url="http://x", api_key=None)
    )
    return fake


@pytest.fixture
def systemctl_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_run(argv, **_):
        calls.append(argv)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(team_module.subprocess, "run", _fake_run)
    return calls


def _run(fake_api, configs, drains, *args) -> int:
    fake_api._state["configs"] = configs
    fake_api._state["drains"] = drains
    result = runner.invoke(team_app, ["sweep", *args])
    assert result.exit_code == 0, result.output
    return result.exit_code


def test_ephemeral_clean_idle_is_swept(fake_api, systemctl_calls) -> None:
    """The positive case: ephemeral + clean + idle beyond grace → torn down."""
    _run(fake_api, [_cfg("o/idle")], {"o/idle": _drain(True, _OLD)})
    # disable --now once per label (coord + eval + 3 workers = 5).
    assert len(systemctl_calls) == 5
    assert all("disable" in c and "--now" in c for c in systemctl_calls)


def test_persistent_is_never_swept(fake_api, systemctl_calls) -> None:
    """A persistent team is idle+clean but must NOT be auto-torn-down."""
    _run(
        fake_api,
        [_cfg("o/keep", lifecycle="persistent")],
        {"o/keep": _drain(True, _OLD)},
    )
    assert systemctl_calls == []


def test_manual_is_never_swept(fake_api, systemctl_calls) -> None:
    """A manual team is torn down only by explicit command, never by the sweep."""
    _run(
        fake_api,
        [_cfg("o/man", lifecycle="manual")],
        {"o/man": _drain(True, _OLD)},
    )
    assert systemctl_calls == []


def test_in_flight_is_not_swept(fake_api, systemctl_calls) -> None:
    """drain-clean is false (team-active work) → the sweep leaves it standing,
    deferring to the drain-guard's in-flight verdict (never reimplemented)."""
    _run(fake_api, [_cfg("o/busy")], {"o/busy": _drain(False, _OLD)})
    assert systemctl_calls == []


def test_within_grace_is_not_swept(fake_api, systemctl_calls) -> None:
    """Clean but last activity is recent → idle-grace holds it (no thrash)."""
    _run(fake_api, [_cfg("o/warm")], {"o/warm": _drain(True, _RECENT)})
    assert systemctl_calls == []


def test_no_activity_is_not_swept(fake_api, systemctl_calls) -> None:
    """A team with no tasks yet (last_activity_at is null) has no age to measure →
    conservatively left alone (could be freshly stood up awaiting its first plan)."""
    _run(fake_api, [_cfg("o/new")], {"o/new": _drain(True, None)})
    assert systemctl_calls == []


def test_dry_run_tears_nothing_down(fake_api, systemctl_calls) -> None:
    """--dry-run reports the candidate but never touches systemd."""
    fake_api._state["configs"] = [_cfg("o/idle")]
    fake_api._state["drains"] = {"o/idle": _drain(True, _OLD)}
    result = runner.invoke(team_app, ["sweep", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []
    assert "o/idle" in result.output


def test_mixed_fleet_sweeps_only_qualifying(fake_api, systemctl_calls) -> None:
    """One qualifying team among several is the only one torn down."""
    configs = [
        _cfg("o/idle"),  # swept
        _cfg("o/keep", lifecycle="persistent"),  # skipped: mode
        _cfg("o/busy"),  # skipped: in-flight
        _cfg("o/warm"),  # skipped: grace
    ]
    drains = {
        "o/idle": _drain(True, _OLD),
        "o/keep": _drain(True, _OLD),
        "o/busy": _drain(False, _OLD),
        "o/warm": _drain(True, _RECENT),
    }
    _run(fake_api, configs, drains)
    # Only o/idle's 5 labels torn down.
    assert len(systemctl_calls) == 5


def test_custom_idle_hours_threshold(fake_api, systemctl_calls) -> None:
    """A team idle 2h is swept with --idle-hours 1 but not with the default (6h)."""
    two_hours_ago = (_NOW - timedelta(hours=2)).isoformat()
    # Default grace (6h) → not swept.
    _run(fake_api, [_cfg("o/two")], {"o/two": _drain(True, two_hours_ago)})
    assert systemctl_calls == []
    # --idle-hours 1 → swept.
    _run(fake_api, [_cfg("o/two")], {"o/two": _drain(True, two_hours_ago)}, "--idle-hours", "1")
    assert len(systemctl_calls) == 5


def test_drain_error_leaves_team_standing(fake_api, systemctl_calls) -> None:
    """Fail-closed: if /drain errors (500/timeout), the sweep CANNOT confirm the
    team is clean, so it MUST NOT tear it down — the team is left standing and
    reported. Autonomous teardown acts only on a POSITIVELY-confirmed clean drain
    (Ernie safety foil)."""
    fake_api._state["configs"] = [_cfg("o/err")]
    fake_api._state["drains"] = {"o/err": ApiError(500, "boom")}
    result = runner.invoke(team_app, ["sweep"])
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []            # never torn down on an unconfirmable drain
    assert "o/err" in result.output          # surfaced as a left-standing drain error


def test_drain_error_does_not_abort_the_rest(fake_api, systemctl_calls) -> None:
    """One team's drain error must not stop the sweep from tearing down a qualifying
    peer — the sweep continues past a per-team error."""
    fake_api._state["configs"] = [_cfg("o/err"), _cfg("o/idle")]
    fake_api._state["drains"] = {
        "o/err": ApiError(503, "unavailable"),
        "o/idle": _drain(True, _OLD),
    }
    result = runner.invoke(team_app, ["sweep"])
    assert result.exit_code == 0, result.output
    # o/idle's 5 labels torn down; o/err left standing.
    assert len(systemctl_calls) == 5
