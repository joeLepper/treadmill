"""Tests for ``treadmill repo add`` (ADR-0085+0086 plan Task F)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from treadmill_cli.commands import repo as repo_module
from treadmill_cli.commands.repo import repo_app


runner = CliRunner()


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def teams_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~/.treadmill/teams/`` into a per-test tmp dir.

    The module-level constant is captured at import time, so we patch
    the bound name on the module rather than the underlying ``Path.home``
    call site (cleaner).
    """
    teams = tmp_path / "teams"
    monkeypatch.setattr(repo_module, "_TEAMS_DIR", teams)
    return teams


@pytest.fixture
def fake_api_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``ApiClient`` with a context-managed MagicMock recording POSTs."""
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake._request = MagicMock(return_value={"ok": True})

    class _Factory:
        def __init__(self, _config):
            pass

        def __enter__(self_inner):
            return fake

        def __exit__(self_inner, *args):
            return False

    monkeypatch.setattr(repo_module, "ApiClient", _Factory)
    monkeypatch.setattr(
        repo_module, "load_config", lambda: MagicMock(api_url="http://x", api_key=None)
    )
    return fake


@pytest.fixture
def systemctl_success(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture every ``systemctl`` argv; return success (rc=0)."""
    calls: list[list[str]] = []

    def _fake_run(argv, *, capture_output, text, check):
        assert argv[0] == "systemctl"
        calls.append(argv)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(repo_module.subprocess, "run", _fake_run)
    return calls


@pytest.fixture
def systemctl_unavailable(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Simulate systemctl missing (FileNotFoundError)."""
    calls: list[list[str]] = []

    def _fake_run(argv, **_):
        calls.append(argv)
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(repo_module.subprocess, "run", _fake_run)
    return calls


# ── Tests ───────────────────────────────────────────────────────────────


class TestSlugAndLabelDefaults:
    def test_slug_lowercases_and_replaces_slash(self) -> None:
        assert repo_module._slug_from_repo("MediCoderHQ/medicoder-events") == (
            "medicoderhq-medicoder-events"
        )

    def test_coordinator_label_default_uses_slug(self) -> None:
        slug = repo_module._slug_from_repo("joeLepper/treadmill")
        assert repo_module._coordinator_label_default(slug) == "coordinator-joelepper-treadmill"


class TestRepoAddHappyPath:
    def test_invokes_team_configs_post_with_default_workers(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        result = runner.invoke(repo_app, ["joeLepper/treadmill"])
        assert result.exit_code == 0, result.stdout

        # POST /api/v1/team_configs with correct payload.
        fake_api_client._request.assert_called_once()
        method, path = fake_api_client._request.call_args.args
        assert method == "POST"
        assert path == "/api/v1/team_configs"
        body = fake_api_client._request.call_args.kwargs["json"]
        assert body["repo"] == "joeLepper/treadmill"
        assert body["coordinator_label"] == "coordinator-joelepper-treadmill"
        assert body["worker_labels"] == [
            "treadmill-bert",
            "treadmill-donna",
            "treadmill-carla",
        ]

    def test_writes_coordinator_env_with_correct_contents(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TREADMILL_API_URL", "http://api.local:8001")
        result = runner.invoke(repo_app, ["joeLepper/treadmill"])
        assert result.exit_code == 0, result.stdout

        env_path = teams_dir / "joelepper-treadmill" / "coordinator.env"
        assert env_path.exists()
        content = env_path.read_text()
        assert "TREADMILL_ROLE=coordinator\n" in content
        assert "TREADMILL_LABEL=coordinator-joelepper-treadmill\n" in content
        assert "TREADMILL_API_URL=http://api.local:8001\n" in content
        assert "TREADMILL_COORDINATOR_PLANS=\n" in content

    def test_systemctl_enable_then_start_with_correct_unit(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        result = runner.invoke(repo_app, ["joeLepper/treadmill"])
        assert result.exit_code == 0, result.stdout

        unit = "treadmill-channel@coordinator-joelepper-treadmill.service"
        assert systemctl_success == [
            ["systemctl", "--user", "enable", unit],
            ["systemctl", "--user", "start", unit],
        ]

    def test_custom_workers_flag_threads_through(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        result = runner.invoke(
            repo_app,
            [
                "joeLepper/treadmill",
                "--workers",
                "treadmill-bert, treadmill-donna",
            ],
        )
        assert result.exit_code == 0, result.stdout
        body = fake_api_client._request.call_args.kwargs["json"]
        assert body["worker_labels"] == ["treadmill-bert", "treadmill-donna"]

    def test_custom_coordinator_label_overrides_default(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        result = runner.invoke(
            repo_app,
            [
                "joeLepper/treadmill",
                "--coordinator-label",
                "coordinator-custom",
            ],
        )
        assert result.exit_code == 0, result.stdout
        body = fake_api_client._request.call_args.kwargs["json"]
        assert body["coordinator_label"] == "coordinator-custom"
        # Env file should also pick up the custom label.
        env_path = teams_dir / "joelepper-treadmill" / "coordinator.env"
        assert "TREADMILL_LABEL=coordinator-custom\n" in env_path.read_text()


class TestRepoAddInputValidation:
    def test_rejects_repo_without_slash(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        result = runner.invoke(repo_app, ["joeLepper"])
        assert result.exit_code == 1
        # API client must NOT have been called.
        fake_api_client._request.assert_not_called()


class TestRepoAddSystemdUnavailable:
    def test_systemd_missing_warns_but_succeeds(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_unavailable: list[list[str]],
    ) -> None:
        """The command must NOT raise when systemctl is missing —
        team_configs row + env file are the load-bearing pieces; the
        systemd hop can be retried by hand."""
        result = runner.invoke(repo_app, ["joeLepper/treadmill"])
        assert result.exit_code == 0, result.stdout
        env_path = teams_dir / "joelepper-treadmill" / "coordinator.env"
        assert env_path.exists()


class TestRepoAddIdempotency:
    def test_second_invocation_does_not_raise(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        """team_configs is an upsert, env file overwrites, systemctl
        enable/start on an already-enabled unit are no-ops — running
        the command twice must not raise."""
        first = runner.invoke(repo_app, ["joeLepper/treadmill"])
        second = runner.invoke(repo_app, ["joeLepper/treadmill"])
        assert first.exit_code == 0, first.stdout
        assert second.exit_code == 0, second.stdout
        # Two upsert calls; two enable + two start.
        assert fake_api_client._request.call_count == 2
        assert len(systemctl_success) == 4
