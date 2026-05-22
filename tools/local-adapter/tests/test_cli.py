"""Smoke tests for the Typer CLI."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from treadmill_local.cli import app


runner = CliRunner()


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("up", "down", "status", "logs", "docs"):
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


# ── docs commands ──────────────────────────────────────────────────────────────
# The handlers render what docs_sync returns; these guard the CLI presentation
# layer against drift from the doc-rest-api contract (LIST → {doc_path, version},
# GET → content str, PULL → [paths], PUSH → [(path, version)]).


def test_docs_list_renders_doc_path_and_version(monkeypatch):
    import treadmill_local.cli as cli

    monkeypatch.setattr(
        cli, "list_docs",
        lambda api_url, repo: [{"doc_path": "adrs/0001.md", "version": 2}],
    )
    result = runner.invoke(app, ["docs", "list", "--repo", "owner/repo"])
    assert result.exit_code == 0, result.stdout
    assert "adrs/0001.md" in result.stdout
    assert "v2" in result.stdout


def test_docs_list_empty(monkeypatch):
    import treadmill_local.cli as cli

    monkeypatch.setattr(cli, "list_docs", lambda api_url, repo: [])
    result = runner.invoke(app, ["docs", "list", "--repo", "owner/repo"])
    assert result.exit_code == 0
    assert "no docs" in result.stdout


def test_docs_get_prints_content(monkeypatch):
    import treadmill_local.cli as cli

    monkeypatch.setattr(
        cli, "get_doc", lambda api_url, repo, doc_path: "# hello\nbody",
    )
    result = runner.invoke(
        app, ["docs", "get", "AGENT.md", "--repo", "owner/repo"]
    )
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_docs_pull_reports_paths(monkeypatch, tmp_path: Path):
    import treadmill_local.cli as cli

    monkeypatch.setattr(
        cli, "pull", lambda api_url, repo, directory: ["AGENT.md", "adrs/x.md"],
    )
    result = runner.invoke(
        app, ["docs", "pull", "--repo", "owner/repo", "--dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "AGENT.md" in result.stdout
    assert "adrs/x.md" in result.stdout
    assert "2 doc(s) pulled" in result.stdout


def test_docs_push_reports_versions(monkeypatch, tmp_path: Path):
    import treadmill_local.cli as cli

    monkeypatch.setattr(
        cli, "push", lambda api_url, repo, directory: [("AGENT.md", 1)],
    )
    result = runner.invoke(
        app, ["docs", "push", "--repo", "owner/repo", "--dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "AGENT.md" in result.stdout
    assert "v1" in result.stdout
    assert "1 doc(s) pushed" in result.stdout
