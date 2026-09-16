"""Real-git end-to-end for integration_merger + SubprocessGitRunner (ADR-0118 phase 2).

No GitHub: a local bare 'origin' + a working clone exercise integrate_task through the REAL
subprocess runner — proving the merge actually lands on origin and a second run is idempotent
(the ancestry check on real refs). Complements the scripted-runner foils (which pin the
control flow) with a proof the git sequence is correct against real git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from treadmill_api.coordination.git_runner import SubprocessGitRunner
from treadmill_api.coordination.integration_merger import MergeOp, integrate_task


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
    }
    return subprocess.check_output(["git", *args], cwd=cwd, env=env, text=True,
                                   stderr=subprocess.STDOUT)


@pytest.fixture()
def repo(tmp_path: Path):
    """A bare origin with main + an integration branch + a task branch with one commit.
    Returns (working_clone_path, integration_branch, task_head_sha)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main", ".")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main", ".")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", "."); _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")
    # integration branch cut from main
    _git(seed, "push", "origin", "main:refs/heads/joes-agents/test-plan")
    # a task branch off main with a non-conflicting change
    _git(seed, "checkout", "-b", "task1")
    (seed / "feature.txt").write_text("feature\n")
    _git(seed, "add", "."); _git(seed, "commit", "-m", "feature")
    task_head = _git(seed, "rev-parse", "HEAD").strip()
    _git(seed, "push", "origin", "task1")

    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), "work")
    return work, "joes-agents/test-plan", task_head


@pytest.mark.asyncio
async def test_real_git_integrate_then_idempotent(repo):
    work, branch, task_head = repo
    runner = SubprocessGitRunner(str(work))
    op = MergeOp(repo="local/test", task_head=task_head, integration_branch=branch, base="main")

    # First integrate: the task head lands on origin's integration branch.
    assert await integrate_task(runner, op) == "merged"
    _git(work, "fetch", "origin", branch)
    rc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", task_head, f"origin/{branch}"],
        cwd=work, env={"PATH": "/usr/bin:/bin", "HOME": str(work)},
    ).returncode
    assert rc == 0  # the task head is now in the integration branch on origin

    # Second integrate of the same head: idempotent no-op (ancestry check on real refs).
    assert await integrate_task(runner, op) == "already-integrated"
