"""Tests for ``treadmill team up`` (ADR-0087 §Team bootstrap).

Covers: deterministic label derivation, POST body shape, per-session
directory tree creation, ``.session-id`` stub behaviour (created on
first run, preserved across re-runs), env file contents, systemctl
invocations per session, scale-down 409 error surface, ``--force``
forwarding, repo-format validation.

The scale-down GUARD itself is tested at the API layer (see
``services/api/tests/test_routers_team_configs.py``); these CLI tests
focus on how the CLI consumes the API's 409 response.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from treadmill_cli.api_client import ApiError
from treadmill_cli.commands import team as team_module
from treadmill_cli.commands.team import team_app


runner = CliRunner()

# Captured before the autouse ``stub_template_install`` fixture patches
# the module attribute — used by the one integration test that exercises
# the real loader.
_REAL_INSTALL_TEMPLATES = team_module._install_templates


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def stub_template_install(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, int]]:
    """Replace ``_install_templates`` with a recorder.

    Autouse so no test renders real templates into the developer's
    actual ``~/.treadmill/teams/`` — ``install.py``'s ``_TEAMS_ROOT``
    is bound to the real ``Path.home()`` at its import time, outside
    the ``teams_dir`` fixture's patch. The render logic itself is
    covered by ``tools/team-templates/tests/test_install.py``; these
    tests assert the CLI wiring (call + args + failure surface).
    """
    calls: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        team_module,
        "_install_templates",
        lambda slug, workers, pr_base="main": calls.append(
            (slug, workers, pr_base)
        ),
    )
    return calls


@pytest.fixture
def teams_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~/.treadmill/teams/`` into a per-test tmp dir."""
    teams = tmp_path / "teams"
    monkeypatch.setattr(team_module, "_TEAMS_DIR", teams)
    return teams


@pytest.fixture
def fake_api_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``ApiClient`` with a recording MagicMock."""
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


@pytest.fixture
def systemctl_unavailable(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_run(argv, **_):
        calls.append(argv)
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(team_module.subprocess, "run", _fake_run)
    return calls


# ── Label derivation ────────────────────────────────────────────────────


class TestLabelDerivation:
    def test_slug_lowercases_and_replaces_slash(self) -> None:
        assert team_module._slug_from_repo("JoeLepper/Treadmill") == (
            "joelepper-treadmill"
        )

    def test_derive_labels_default_3_workers(self) -> None:
        coord, eval_, workers = team_module._derive_labels("ramjac", 3)
        assert coord == "coordinator-ramjac"
        assert eval_ == "evaluator-ramjac"
        assert workers == [
            "worker-ramjac-1",
            "worker-ramjac-2",
            "worker-ramjac-3",
        ]

    def test_derive_labels_custom_count(self) -> None:
        coord, eval_, workers = team_module._derive_labels("ramjac", 5)
        assert len(workers) == 5
        assert workers[-1] == "worker-ramjac-5"

    def test_role_for_label_inference(self) -> None:
        assert team_module._role_for_label("coordinator-x") == "coordinator"
        assert team_module._role_for_label("evaluator-x") == "evaluator"
        assert team_module._role_for_label("worker-x-1") == "worker"
        assert team_module._role_for_label("worker-ramjac-3") == "worker"


# ── Happy path ───────────────────────────────────────────────────────────


class TestTeamUpHappyPath:
    def test_posts_team_config_with_derived_labels(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        result = runner.invoke(team_app, ["joeLepper/treadmill"])
        assert result.exit_code == 0, result.stdout

        fake_api_client._request.assert_called_once()
        method, path = fake_api_client._request.call_args.args
        assert method == "POST"
        assert path == "/api/v1/team_configs"
        body = fake_api_client._request.call_args.kwargs["json"]
        assert body["repo"] == "joeLepper/treadmill"
        assert body["coordinator_label"] == "coordinator-joelepper-treadmill"
        assert body["evaluator_label"] == "evaluator-joelepper-treadmill"
        assert body["worker_labels"] == [
            "worker-joelepper-treadmill-1",
            "worker-joelepper-treadmill-2",
            "worker-joelepper-treadmill-3",
        ]

    def test_creates_per_session_directory_tree(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        result = runner.invoke(team_app, ["x/y"])
        assert result.exit_code == 0, result.stdout

        team_root = teams_dir / "x-y"
        assert team_root.is_dir()
        for label in (
            "coordinator-x-y",
            "evaluator-x-y",
            "worker-x-y-1",
            "worker-x-y-2",
            "worker-x-y-3",
        ):
            session_dir = team_root / label
            assert session_dir.is_dir(), f"missing {session_dir}"
            session_id = session_dir / ".session-id"
            env = session_dir / f"{label}.env"
            assert session_id.exists()
            assert session_id.read_text() == ""  # empty stub on creation
            assert env.exists()
            assert f"TREADMILL_LABEL={label}\n" in env.read_text()

    def test_env_files_have_correct_role(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        runner.invoke(team_app, ["x/y"])
        coord_env = (teams_dir / "x-y" / "coordinator-x-y" / "coordinator-x-y.env").read_text()
        eval_env = (teams_dir / "x-y" / "evaluator-x-y" / "evaluator-x-y.env").read_text()
        worker_env = (teams_dir / "x-y" / "worker-x-y-1" / "worker-x-y-1.env").read_text()
        assert "TREADMILL_ROLE=coordinator\n" in coord_env
        assert "TREADMILL_ROLE=evaluator\n" in eval_env
        assert "TREADMILL_ROLE=worker\n" in worker_env

    def test_systemctl_enable_then_start_per_session(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        runner.invoke(team_app, ["x/y", "--workers", "2"])
        # Each of 4 sessions (1 coord + 1 eval + 2 workers) gets enable + start.
        verbs_per_unit = [a[2] for a in systemctl_success]
        # 4 sessions × 2 verbs = 8 invocations
        assert len(systemctl_success) == 8
        units = [a[3] for a in systemctl_success]
        assert "treadmill-channel@coordinator-x-y.service" in units
        assert "treadmill-channel@evaluator-x-y.service" in units
        assert "treadmill-channel@worker-x-y-1.service" in units
        assert "treadmill-channel@worker-x-y-2.service" in units

    def test_custom_workers_count(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        runner.invoke(team_app, ["x/y", "--workers", "5"])
        body = fake_api_client._request.call_args.kwargs["json"]
        assert len(body["worker_labels"]) == 5
        assert body["worker_labels"][-1] == "worker-x-y-5"


# ── Idempotency ─────────────────────────────────────────────────────────


class TestIdempotency:
    def test_session_id_preserved_on_re_run(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        """A populated ``.session-id`` (post-first-spawn captured by
        the coordinator) is NOT overwritten on a re-run. Re-creating
        the stub would lose the worker's accumulated memory.
        """
        # First run creates the stub.
        runner.invoke(team_app, ["x/y", "--workers", "1"])
        worker_session_id = (
            teams_dir / "x-y" / "worker-x-y-1" / ".session-id"
        )
        # Simulate the coordinator capturing the real session ID.
        worker_session_id.write_text("captured-session-uuid")

        # Re-run team up. Session ID file MUST survive.
        runner.invoke(team_app, ["x/y", "--workers", "1"])
        assert worker_session_id.read_text() == "captured-session-uuid"


# ── Scale-down handling ─────────────────────────────────────────────────


class TestScaleDown:
    def test_scale_down_409_surfaces_error_and_exits_nonzero(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        """When the API returns 409 (scale-down would orphan
        in-flight task_executions), the CLI exits non-zero with a
        clear error pointing at ``--force``."""
        fake_api_client._request.side_effect = ApiError(
            status_code=409,
            detail="scale-down would orphan in-flight task_executions on worker labels ['w-3']: ['uuid-1']",
        )
        result = runner.invoke(team_app, ["x/y", "--workers", "2"])
        assert result.exit_code == 2  # CLI's typer.Exit(code=2) on API error

    def test_force_flag_forwards_query_param(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        """``--force`` appends ``?force=true`` to the upsert URL so the
        server-side guard short-circuits."""
        result = runner.invoke(team_app, ["x/y", "--workers", "2", "--force"])
        assert result.exit_code == 0, result.stdout
        _method, path = fake_api_client._request.call_args.args
        assert path == "/api/v1/team_configs?force=true"


# ── Input validation ────────────────────────────────────────────────────


class TestInputValidation:
    def test_rejects_repo_without_slash(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        result = runner.invoke(team_app, ["joelepper"])
        assert result.exit_code == 1
        # API client was never called.
        fake_api_client._request.assert_not_called()

    def test_workers_min_1(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        """Typer's ``min=1`` constraint catches zero-worker invocations
        before any work fires."""
        result = runner.invoke(team_app, ["x/y", "--workers", "0"])
        assert result.exit_code != 0
        fake_api_client._request.assert_not_called()


# ── Systemd unavailable ──────────────────────────────────────────────────


class TestSystemdUnavailable:
    def test_systemd_missing_warns_but_succeeds(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_unavailable: list[list[str]],
    ) -> None:
        """No systemd (CI / container / macOS) is not fatal — the
        team_configs row + directory tree are the load-bearing
        artifacts."""
        result = runner.invoke(team_app, ["x/y", "--workers", "1"])
        assert result.exit_code == 0, result.stdout
        # Directory tree is still populated.
        assert (teams_dir / "x-y" / "coordinator-x-y" / ".session-id").exists()
        assert (teams_dir / "x-y" / "evaluator-x-y" / ".session-id").exists()
        assert (teams_dir / "x-y" / "worker-x-y-1" / ".session-id").exists()


# ── Template render wiring (ADR-0087 PR-D/E/H CLI follow-up) ─────────────


class TestTemplateInstallWiring:
    def test_up_calls_install_templates_with_slug_and_workers(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
        stub_template_install: list[tuple[str, int]],
    ) -> None:
        """``team up`` renders the per-session templates — the wiring
        whose absence meant sessions booted with no CLAUDE.md and no
        ``.claude/settings.json`` (so the PostToolUse relay-inject hook
        never registered). See docs/learnings/
        2026-06-10-template-install-layout-vs-launcher-cwd-mismatch.md."""
        result = runner.invoke(team_app, ["Acme/Widget", "--workers", "2"])
        assert result.exit_code == 0, result.stdout
        assert stub_template_install == [("acme-widget", 2, "main")]


class TestOsmoFlags:
    """``--slug`` / ``--pr-base`` / ``--env-extra`` — the new-team-type
    extension (zephyr). Default behaviour (no flags) is asserted unchanged
    by the rest of the suite."""

    def test_pr_base_forwarded_to_install(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
        stub_template_install: list[tuple[str, int, str]],
    ) -> None:
        result = runner.invoke(
            team_app, ["ZEPHYR/zephyr", "--pr-base", "forecast/stage-a"]
        )
        assert result.exit_code == 0, result.stdout
        assert stub_template_install[0][2] == "forecast/stage-a"

    def test_slug_override_decouples_labels_from_repo(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
        stub_template_install: list[tuple[str, int, str]],
    ) -> None:
        result = runner.invoke(
            team_app, ["ZEPHYR/zephyr", "--slug", "joelepper-zephyr"]
        )
        assert result.exit_code == 0, result.stdout
        # labels use the override slug; team_configs.repo stays the real repo
        body = fake_api_client._request.call_args.kwargs["json"]
        assert body["repo"] == "ZEPHYR/zephyr"
        assert body["coordinator_label"] == "coordinator-joelepper-zephyr"
        assert (teams_dir / "joelepper-zephyr").is_dir()

    def test_env_extra_appended_to_label_env(
        self,
        tmp_path: Path,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
        stub_template_install: list[tuple[str, int, str]],
    ) -> None:
        extra = tmp_path / "zephyr.env"
        extra.write_text("source /home/joe/zephyr/.env\nZEPHYR_PYTHON=/home/joe/zephyr/tools/zephyr-python\n")
        result = runner.invoke(
            team_app,
            ["ZEPHYR/zephyr", "--slug", "joelepper-zephyr", "--env-extra", str(extra)],
        )
        assert result.exit_code == 0, result.stdout
        env_body = (
            teams_dir / "joelepper-zephyr" / "worker-joelepper-zephyr-1"
            / "worker-joelepper-zephyr-1.env"
        ).read_text()
        assert "TREADMILL_ROLE=worker" in env_body          # standard vars still present
        assert "ZEPHYR_PYTHON=/home/joe/zephyr/tools/zephyr-python" in env_body  # extra appended

    def test_env_extra_missing_file_errors(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
    ) -> None:
        result = runner.invoke(
            team_app, ["ZEPHYR/zephyr", "--env-extra", "/nonexistent/zephyr.env"]
        )
        assert result.exit_code == 1

    def test_template_failure_aborts_with_exit_2_before_systemd(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A render failure is a hard abort (exit 2) and the systemd
        start never fires — booting sessions without templates is the
        silent no-op PR-H exists to prevent."""

        def _boom(slug: str, workers: int) -> None:
            raise FileNotFoundError("install.py not found")

        monkeypatch.setattr(team_module, "_install_templates", _boom)
        result = runner.invoke(team_app, ["x/y", "--workers", "1"])
        assert result.exit_code == 2
        assert systemctl_success == []  # no enable/start attempted

    def test_repo_root_honors_env_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("TREADMILL_REPO_DIR", str(tmp_path))
        assert team_module._repo_root() == tmp_path

    def test_repo_root_derives_from_module_location(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the env override, the repo root is three levels up
        from ``commands/team.py`` — the editable-install layout."""
        monkeypatch.delenv("TREADMILL_REPO_DIR", raising=False)
        root = team_module._repo_root()
        assert (root / "cli" / "treadmill_cli" / "commands" / "team.py").is_file()

    def test_install_templates_real_module_load_and_render(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Integration: ``_install_templates`` loads the real
        ``tools/team-templates/install.py`` and renders to disk.
        install.py resolves ``_TEAMS_ROOT`` from ``Path.home()`` at
        import time and ``_install_templates`` imports it fresh per
        call, so patching ``HOME`` redirects the render. Uses the
        pre-stub captured function (the autouse fixture replaces the
        module attribute)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # pathlib.Path.home() reads HOME on POSIX.
        _REAL_INSTALL_TEMPLATES("acme-widget", 1)
        team_dir = tmp_path / ".treadmill" / "teams" / "acme-widget"
        assert (team_dir / "coordinator-acme-widget" / "CLAUDE.md").is_file()
        assert (
            team_dir / "worker-acme-widget-1" / ".claude" / "settings.json"
        ).is_file()


# ── Per-role model pin (task 5d14fbcc — INCIDENT 2026-06-12) ──────────────


class TestModelPin:
    """ANTHROPIC_MODEL written per-role into each .env so --resume never
    falls back to the account-default (claude-fable-5 unavailable on this
    account — incident 2026-06-12: coordinator + evaluator sessions silently
    died; workers were safe only because their cwd .claude/settings.json
    pinned sonnet; settings.json "model" does NOT fix --resume's persisted
    model selection — only the env-var path reaches the subprocess)."""

    def test_coordinator_env_carries_opus_model(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        runner.invoke(team_app, ["x/y"])
        env_body = (
            teams_dir / "x-y" / "coordinator-x-y" / "coordinator-x-y.env"
        ).read_text()
        assert "ANTHROPIC_MODEL=claude-opus-4-8\n" in env_body

    def test_evaluator_env_carries_opus_model(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        runner.invoke(team_app, ["x/y"])
        env_body = (
            teams_dir / "x-y" / "evaluator-x-y" / "evaluator-x-y.env"
        ).read_text()
        assert "ANTHROPIC_MODEL=claude-opus-4-8\n" in env_body

    def test_worker_env_carries_sonnet_model(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        runner.invoke(team_app, ["x/y"])
        env_body = (
            teams_dir / "x-y" / "worker-x-y-1" / "worker-x-y-1.env"
        ).read_text()
        assert "ANTHROPIC_MODEL=claude-sonnet-4-6\n" in env_body

    def test_model_pin_is_distinct_per_role(
        self,
        teams_dir: Path,
        fake_api_client: MagicMock,
        systemctl_success: list[list[str]],
    ) -> None:
        """Coordinator + evaluator get opus; workers get sonnet — not
        the same value, so a single-value regression can't pass silently."""
        runner.invoke(team_app, ["x/y"])
        coord_model = (
            teams_dir / "x-y" / "coordinator-x-y" / "coordinator-x-y.env"
        ).read_text()
        worker_model = (
            teams_dir / "x-y" / "worker-x-y-1" / "worker-x-y-1.env"
        ).read_text()
        assert "claude-opus-4-8" in coord_model
        assert "claude-sonnet-4-6" in worker_model
        assert "claude-opus-4-8" not in worker_model
        assert "claude-sonnet-4-6" not in coord_model
