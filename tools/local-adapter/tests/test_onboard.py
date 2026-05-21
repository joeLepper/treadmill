"""Unit tests for ``treadmill_local.onboard`` (ADR-0051).

Pure-Python tests: no network, no real git. We drive ``build_profile``
against fixtures laid out under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from treadmill_local.onboard import build_profile, infer_repo, onboard_payload


# ─── infer_repo ──────────────────────────────────────────────────────────


def test_infer_repo_ssh_form() -> None:
    assert infer_repo("git@github.com:owner/name.git") == "owner/name"


def test_infer_repo_ssh_form_no_dot_git() -> None:
    assert infer_repo("git@github.com:owner/name") == "owner/name"


def test_infer_repo_https_form() -> None:
    assert infer_repo("https://github.com/owner/name") == "owner/name"


def test_infer_repo_https_form_with_dot_git() -> None:
    assert infer_repo("https://github.com/owner/name.git") == "owner/name"


def test_infer_repo_https_form_trailing_slash() -> None:
    assert infer_repo("https://github.com/owner/name/") == "owner/name"


def test_infer_repo_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        infer_repo("not-a-url")


def test_infer_repo_rejects_empty() -> None:
    with pytest.raises(ValueError):
        infer_repo("   ")


# ─── build_profile ───────────────────────────────────────────────────────


def test_build_profile_minimal_tree(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# x\n")
    (tmp_path / "main.py").write_text("print('hi')\n")
    profile = build_profile(tmp_path)
    assert profile["has_agent_context"] is False
    assert profile["ci"] is None
    assert "python" in profile["languages"]
    assert "README.md" in profile["doc_paths"]


def test_build_profile_agent_context_flips_when_agent_md_exists(tmp_path: Path) -> None:
    (tmp_path / "AGENT.md").write_text("# agent\n")
    profile = build_profile(tmp_path)
    assert profile["has_agent_context"] is True
    assert "AGENT.md" in profile["doc_paths"]


def test_build_profile_agent_context_detects_nested_agent_md(tmp_path: Path) -> None:
    sub = tmp_path / "services" / "api"
    sub.mkdir(parents=True)
    (sub / "AGENT.md").write_text("# nested\n")
    profile = build_profile(tmp_path)
    assert profile["has_agent_context"] is True


def test_build_profile_detects_github_actions_ci(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: ci\n")
    profile = build_profile(tmp_path)
    assert profile["ci"] == "github-actions"


def test_build_profile_no_ci_when_no_workflows(tmp_path: Path) -> None:
    profile = build_profile(tmp_path)
    assert profile["ci"] is None


def test_build_profile_pyproject_picks_uv_test_command(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    profile = build_profile(tmp_path)
    assert profile["test_command"] == "uv run pytest"


def test_build_profile_package_json_picks_npm_commands(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}\n")
    profile = build_profile(tmp_path)
    assert profile["test_command"] == "npm test"
    assert profile["build_command"] == "npm run build"
    assert profile["lint_command"] == "npm run lint"


def test_build_profile_no_markers_yields_none_commands(tmp_path: Path) -> None:
    profile = build_profile(tmp_path)
    assert profile["build_command"] is None
    assert profile["test_command"] is None
    assert profile["lint_command"] is None


def test_build_profile_components_lists_top_level_dirs(tmp_path: Path) -> None:
    (tmp_path / "services").mkdir()
    (tmp_path / "workers").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    profile = build_profile(tmp_path)
    assert "services" in profile["components"]
    assert "workers" in profile["components"]
    assert ".git" not in profile["components"]
    assert "node_modules" not in profile["components"]


def test_build_profile_languages_skip_vendored_dirs(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n")
    nm = tmp_path / "node_modules" / "junk"
    nm.mkdir(parents=True)
    for i in range(5):
        (nm / f"file{i}.js").write_text("x\n")
    profile = build_profile(tmp_path)
    # node_modules contents are skipped; only the root .py file counts.
    assert profile["languages"] == ["python"]


# ─── onboard_payload ─────────────────────────────────────────────────────


def test_onboard_payload_shape() -> None:
    profile = {
        "languages": ["python"],
        "build_command": None,
        "test_command": "uv run pytest",
        "lint_command": None,
        "doc_paths": ["README.md"],
        "components": ["services"],
        "ci": "github-actions",
        "has_agent_context": True,
    }
    body = onboard_payload(
        "owner/name", profile, mode=None, auto_merge_blocked=True,
    )
    assert body["repo"] == "owner/name"
    assert body["mode"] is None
    assert body["auto_merge_blocked"] is True
    # profile carries its own repo key per the contract.
    assert body["profile"]["repo"] == "owner/name"
    assert body["profile"]["languages"] == ["python"]
    assert body["profile"]["has_agent_context"] is True


def test_onboard_payload_with_explicit_mode() -> None:
    body = onboard_payload(
        "owner/name",
        {"languages": []},
        mode="adapt",
        auto_merge_blocked=False,
    )
    assert body["mode"] == "adapt"
    assert body["auto_merge_blocked"] is False
