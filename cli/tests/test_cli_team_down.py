"""CLI foils for ``treadmill team down`` (ADR-0109 step 1).

The drain-guard's DB logic is tested server-side; these pin the CLI contract: it
REFUSES (exit 2) on blocking work and does NOT touch systemd; it tears down (stop +
disable every unit) when clean or forced; parked-on-human work does NOT block.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from treadmill_cli.api_client import ApiError
from treadmill_cli.commands import team as team_module
from treadmill_cli.commands.team import team_app

runner = CliRunner()

_CONFIG = {
    "coordinator_label": "coordinator-o-r",
    "evaluator_label": "evaluator-o-r",
    "worker_labels": ["worker-o-r-1", "worker-o-r-2", "worker-o-r-3"],
}


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()

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


def _drain(clean: bool, blocking=None, parked=None) -> dict:
    return {"repo": "o/r", "clean": clean, "blocking": blocking or [], "parked": parked or []}


def test_clean_drain_tears_down_all_units(fake_api, systemctl_calls) -> None:
    fake_api._request.side_effect = [_CONFIG, _drain(clean=True)]
    result = runner.invoke(team_app, ["down", "o/r"])
    assert result.exit_code == 0, result.output
    # disable --now fired once per label (coordinator + evaluator + 3 workers = 5).
    assert len(systemctl_calls) == 5
    assert all("disable" in c and "--now" in c for c in systemctl_calls)


def test_blocking_drain_refuses_and_leaves_systemd_untouched(fake_api, systemctl_calls) -> None:
    fake_api._request.side_effect = [
        _CONFIG,
        _drain(clean=False, blocking=[
            {"task_id": "t1", "derived_status": "wf: executing", "reason": "team_active"}
        ]),
    ]
    result = runner.invoke(team_app, ["down", "o/r"])
    assert result.exit_code == 2, result.output
    assert systemctl_calls == []  # never stops a team with in-flight work


def test_force_tears_down_despite_blocking(fake_api, systemctl_calls) -> None:
    fake_api._request.side_effect = [
        _CONFIG,
        _drain(clean=False, blocking=[
            {"task_id": "t1", "derived_status": "wf: executing", "reason": "team_active"}
        ]),
    ]
    result = runner.invoke(team_app, ["down", "o/r", "--force"])
    assert result.exit_code == 0, result.output
    assert len(systemctl_calls) == 5


def test_parked_only_does_not_block(fake_api, systemctl_calls) -> None:
    # An escalated (parked-on-human) task with clean=True must NOT block teardown.
    fake_api._request.side_effect = [_CONFIG, _drain(clean=True, parked=["t-escalated"])]
    result = runner.invoke(team_app, ["down", "o/r"])
    assert result.exit_code == 0, result.output
    assert len(systemctl_calls) == 5


def test_missing_team_config_is_noop(fake_api, systemctl_calls) -> None:
    fake_api._request.side_effect = ApiError(404, "not found")
    result = runner.invoke(team_app, ["down", "o/r"])
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []


def test_non_slash_repo_rejected(fake_api, systemctl_calls) -> None:
    result = runner.invoke(team_app, ["down", "justname"])
    assert result.exit_code == 1
    assert systemctl_calls == []


@pytest.fixture(autouse=True)
def _clear_invoker_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the invoking session to EXTERNAL (no member label) so the self-kill
    guard is deterministic; the guard tests set TREADMILL_LABEL explicitly."""
    monkeypatch.delenv("TREADMILL_LABEL", raising=False)


@pytest.mark.parametrize(
    "member_label",
    ["coordinator-o-r", "evaluator-o-r", "worker-o-r-2"],
)
def test_self_kill_guard_refuses_a_team_member(
    fake_api, systemctl_calls, monkeypatch, member_label
) -> None:
    """ADR-0112 self-kill guard (defense-in-depth): a session whose TREADMILL_LABEL is
    ANY of the team's labels — coordinator, EVALUATOR, or a worker — cannot run
    `team down` on its own team; refuse (exit 2) and touch no systemd. Enforced by the
    tool, not just the caller's self-check. Includes the evaluator (Ernie: the role a
    hand-enumerated check drops)."""
    fake_api._request.side_effect = [_CONFIG]  # never reaches the drain call
    monkeypatch.setenv("TREADMILL_LABEL", member_label)
    result = runner.invoke(team_app, ["down", "o/r"])
    assert result.exit_code == 2, result.output
    assert systemctl_calls == []
    assert "self-kill" in result.output


def test_self_kill_guard_not_overridden_by_force(
    fake_api, systemctl_calls, monkeypatch
) -> None:
    """--force overrides the DRAIN-guard, never the self-kill guard."""
    fake_api._request.side_effect = [_CONFIG]
    monkeypatch.setenv("TREADMILL_LABEL", "worker-o-r-1")
    result = runner.invoke(team_app, ["down", "o/r", "--force"])
    assert result.exit_code == 2, result.output
    assert systemctl_calls == []


def test_external_actor_passes_self_kill_guard(fake_api, systemctl_calls, monkeypatch) -> None:
    """An external actor (an orchestrator label) is not a member → guard passes,
    teardown proceeds on a clean drain."""
    fake_api._request.side_effect = [_CONFIG, _drain(clean=True)]
    monkeypatch.setenv("TREADMILL_LABEL", "treadmill-alan")
    result = runner.invoke(team_app, ["down", "o/r"])
    assert result.exit_code == 0, result.output
    assert len(systemctl_calls) == 5
