"""Workspace-management tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from treadmill_agent.workspace import workspace_for_step


def test_workspace_creates_and_cleans(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    with workspace_for_step(str(root), "step-x") as path:
        assert path.is_dir()
        (path / "f.txt").write_text("x")
    assert not path.exists()


def test_workspace_replaces_existing(tmp_path: Path) -> None:
    """If the per-step dir already has stale content, it's wiped clean
    before the worker starts. Same step_id with different prior content
    must not leak data into the new run."""
    root = tmp_path / "ws"
    leftover = root / "step-x"
    leftover.mkdir(parents=True)
    (leftover / "stale.txt").write_text("stale")
    with workspace_for_step(str(root), "step-x") as path:
        assert not (path / "stale.txt").exists()


def test_workspace_kept_when_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEEP_WORKSPACES", "1")
    root = tmp_path / "ws"
    with workspace_for_step(str(root), "step-x") as path:
        (path / "f.txt").write_text("x")
    # Preserved for inspection.
    assert path.exists()
    assert (path / "f.txt").exists()
