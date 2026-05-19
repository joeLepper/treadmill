"""Smoke tests for the Typer CLI."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from treadmill_local.cli import app


runner = CliRunner()


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("up", "down", "status", "logs"):
        assert cmd in result.stdout


def test_up_with_missing_infra_dir_exits_cleanly(tmp_path: Path):
    """If --infra points at a directory without cdk.json, exit code 2."""
    result = runner.invoke(app, ["up", "--infra", str(tmp_path)])
    assert result.exit_code == 2
    assert "cdk.json" in result.stdout


def test_cli_callback_anchors_cwd_to_repo_root(
    tmp_path: Path, monkeypatch
) -> None:
    """The CLI callback chdirs to the repo root so that ``STATE_DIR`` (a
    relative path) always resolves to ``<repo>/.treadmill-local/`` no matter
    where the operator invoked ``treadmill-local`` from. Without this, e.g.
    ``uv run --project tools/local-adapter ...`` wrote state to
    ``tools/local-adapter/.treadmill-local/`` and operators saw "watcher
    crash loop" symptoms while logs landed in a sibling directory.

    Tests the callback directly because typer's ``--help`` short-circuits
    before callbacks fire, and any real subcommand requires Docker etc.
    """
    from treadmill_local import runtime as runtime_module
    from treadmill_local.cli import _chdir_to_repo_root

    monkeypatch.chdir(tmp_path)
    assert Path.cwd() == tmp_path

    _chdir_to_repo_root()

    assert Path.cwd() == runtime_module.find_repo_root()
