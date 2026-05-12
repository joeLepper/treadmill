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
