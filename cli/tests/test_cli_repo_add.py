"""Tests for the DEPRECATED ``treadmill repo add`` alias.

Per ADR-0087, ``treadmill repo add`` is a compatibility alias for
``treadmill team up``. The alias delegates to :func:`treadmill_cli.
commands.team.up` after emitting a deprecation warning. Comprehensive
coverage of the underlying ``team up`` command lives in
:mod:`tests.test_cli_team_up`; this file pins the alias contract
(warning + forwarding).
"""

from __future__ import annotations

import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from treadmill_cli.commands import repo as repo_module
from treadmill_cli.commands import team as team_module
from treadmill_cli.commands.repo import repo_app


runner = CliRunner()


# ── Fixtures (mirrors test_cli_team_up.py — both target the same delegate) ──


@pytest.fixture
def teams_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~/.treadmill/teams/`` into a per-test tmp dir.

    ``_TEAMS_DIR`` is captured at module import time as a Path; we
    rebind on the team module (the underlying delegate) so the alias's
    forwarding picks up the redirect.
    """
    teams = tmp_path / "teams"
    monkeypatch.setattr(team_module, "_TEAMS_DIR", teams)
    return teams


@pytest.fixture
def fake_api_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    fake._request = MagicMock(return_value={"ok": True})

    class _Factory:
        def __init__(self, _config):
            pass

        def __enter__(self_inner):
            return fake

        def __exit__(self_inner, *args):
            return False

    monkeypatch.setattr(team_module, "ApiClient", _Factory)
    monkeypatch.setattr(
        team_module,
        "load_config",
        lambda: MagicMock(api_url="http://x", api_key=None),
    )
    return fake


@pytest.fixture
def systemctl_success(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_run(argv, *, capture_output, text, check):
        assert argv[0] == "systemctl"
        calls.append(argv)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(team_module.subprocess, "run", _fake_run)
    return calls


# ── Tests ───────────────────────────────────────────────────────────────


class TestDeprecationAlias:
    def test_repo_add_forwards_to_team_up_with_deprecation(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        """``treadmill repo add <repo>`` forwards verbatim + warns.

        The exit code is the team-up exit code; the side effects (API
        POST + directory tree + systemctl) are the team-up side
        effects. The deprecation warning lands on the python-side
        warning channel.
        """
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            result = runner.invoke(repo_app, ["joeLepper/treadmill"])

        assert result.exit_code == 0, result.stdout
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "treadmill repo add" in str(w.message)
            for w in captured
        ), "expected a DeprecationWarning for repo add"

        # Forwarding worked: the underlying team_up did its work.
        fake_api_client._request.assert_called_once()
        # And it posted with the ADR-0087 derived-labels shape, not the
        # pre-ADR-0087 repo-add payload.
        body = fake_api_client._request.call_args.kwargs["json"]
        assert "evaluator_label" in body
        assert body["evaluator_label"] == "evaluator-joelepper-treadmill"

    def test_repo_add_workers_flag_threads_through(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        """``--workers N`` forwards verbatim to team-up's worker count."""
        result = runner.invoke(
            repo_app, ["joeLepper/treadmill", "--workers", "2"]
        )
        assert result.exit_code == 0, result.stdout
        body = fake_api_client._request.call_args.kwargs["json"]
        assert body["worker_labels"] == [
            "worker-joelepper-treadmill-1",
            "worker-joelepper-treadmill-2",
        ]
