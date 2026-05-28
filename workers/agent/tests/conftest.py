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

import pytest

from treadmill_agent import git


@pytest.fixture(autouse=True)
def _default_claude_creds_off(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``fetch_claude_credentials`` to "feature off" (ADR-0055).

    The runner now calls ``startup_auth.fetch_claude_credentials`` for every
    step. Without this autouse default, every existing test would try to hit
    a real API endpoint via urllib and fail with a DNS error. Tests that
    exercise per-account routing override this with their own monkeypatch.

    Skipped for ``test_startup_auth`` which exercises the real function
    directly — shadowing it there would defeat the tests' purpose.
    """
    if request.module.__name__.endswith("test_startup_auth"):
        return
    from treadmill_agent import startup_auth

    monkeypatch.setattr(
        startup_auth, "fetch_claude_credentials",
        lambda *, settings, repo: None,
    )


@pytest.fixture(autouse=True)
def _default_repo_deps_empty(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``fetch_repo_worker_deps`` to empty (ADR-0059 step 2).

    Same rationale as ``_default_claude_creds_off``: the runner now calls
    ``startup_auth.fetch_repo_worker_deps`` for every step. Without an
    autouse default, every existing test would try to hit the onboarding
    API via urllib. Tests that exercise materialization specifically
    (``test_repo_deps``) call ``repo_deps.materialize`` directly and
    don't route through this seam.
    """
    if request.module.__name__.endswith("test_startup_auth"):
        return
    from treadmill_api.models.onboarding import WorkerDeps

    from treadmill_agent import startup_auth

    monkeypatch.setattr(
        startup_auth, "fetch_repo_worker_deps",
        lambda settings, repo: WorkerDeps(),
    )



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
