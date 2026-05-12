"""Shared fixtures + helpers for the worker test suite.

The bare-repo seeding helper is duplicated across ``test_git.py`` and
``test_runner.py``. Centralizing here keeps the seeding behavior
identical across tests so a divergence can't quietly hide a contract
drift, while the existing inline copies stay put (lower-risk: changing
those would touch tests outside Phase 4's scope).

The Phase 4 capstone tests (B.11 real-Claude smoke + B.12 worker
container integration) import ``init_bare_repo`` from here so they share
the canonical seeding shape with the rest of the suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from treadmill_agent import git


def init_bare_repo(bare_repos_dir: Path, repo: str) -> Path:
    """Seed a bare repo with one ``main`` commit and return its path.

    Mirrors the per-test helper in ``test_git.py`` / ``test_runner.py``
    so all tests that touch the local-mode git path agree on the
    starting state (single ``README.md`` on ``main``).

    Idempotent — if the bare repo already exists it is left alone.
    """
    bare_repos_dir.mkdir(parents=True, exist_ok=True)
    bare = (bare_repos_dir / f"{git.repo_to_directory_name(repo)}.git").resolve()
    if bare.exists():
        return bare
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True,
    )
    seed = bare_repos_dir.parent / f"seed-{abs(hash(repo)) & 0xffff:x}"
    if seed.exists():
        shutil.rmtree(seed)
    seed.mkdir()
    try:
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(seed)], check=True,
        )
        (seed / "README.md").write_text("# repo\n")
        for cmd in (
            ["git", "-C", str(seed), "config", "user.email", "t@t"],
            ["git", "-C", str(seed), "config", "user.name", "t"],
            ["git", "-C", str(seed), "add", "-A"],
            ["git", "-C", str(seed), "commit", "-m", "init"],
            ["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
            ["git", "-C", str(seed), "push", "origin", "main"],
        ):
            subprocess.run(cmd, check=True)
    finally:
        shutil.rmtree(seed, ignore_errors=True)
    return bare
