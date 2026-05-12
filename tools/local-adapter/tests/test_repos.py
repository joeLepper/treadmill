"""Bare-repo provisioning tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from treadmill_local.repos import init_bare_repo


def test_init_creates_bare_repo_with_main_branch(tmp_path: Path) -> None:
    bare = init_bare_repo(tmp_path / "repos", "owner/name")
    assert bare.exists()
    assert bare.name == "owner__name.git"
    # Confirm main branch is set up by listing refs.
    out = subprocess.run(
        ["git", "-C", str(bare), "branch"],
        capture_output=True, text=True, check=True,
    )
    assert "main" in out.stdout


def test_init_is_idempotent(tmp_path: Path) -> None:
    bare1 = init_bare_repo(tmp_path / "repos", "owner/name")
    # Mark a custom file inside the bare repo to detect rewrite.
    marker = bare1 / "treadmill-marker"
    marker.write_text("preserved")

    bare2 = init_bare_repo(tmp_path / "repos", "owner/name")
    assert bare1 == bare2
    # Running init again does not blow away the existing repo.
    assert marker.read_text() == "preserved"


def test_init_clone_works(tmp_path: Path) -> None:
    """The seed commit means ``git clone`` produces a populated work tree."""
    bare = init_bare_repo(tmp_path / "repos", "x/y")
    out = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", str(bare), str(out)],
        capture_output=True, check=True,
    )
    assert (out / "README.md").exists()
