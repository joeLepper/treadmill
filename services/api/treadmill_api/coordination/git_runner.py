"""Subprocess GitRunner — the prod executor behind ``integration_merger`` (ADR-0118 phase 2).

``integration_merger`` runs its git sequence through the ``GitRunner`` protocol; the foils
inject a scripted runner, prod uses this one: real ``git`` in a per-repo working clone. Kept
separate from the merger so the merge LOGIC stays pure-runner and DB-free (the pure-core split
used across the router).

Working clone: one long-lived clone per repo under a router-owned state dir, fetched (not
re-cloned) on each op. Credentials are ambient — the same ``git``/``gh`` config the fleet
already uses to push (no new secret handling here). Fetch prefers the PR ref
(``refs/pull/<n>/head``) over a bare SHA, which a server may not have reachable (Bert #2).
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class SubprocessGitRunner:
    """Runs ``git`` in a fixed working directory. Implements the ``GitRunner`` protocol
    (``run(*args) -> (rc, combined_output)``). stderr is folded into stdout so a caller sees
    the whole message (the merger's conflict-vs-infra check reads it)."""

    def __init__(self, cwd: str, *, timeout: float = 120.0) -> None:
        self._cwd = cwd
        self._timeout = timeout

    async def run(self, *args: str) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self._cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            rc = proc.returncode if proc.returncode is not None else 1
            return rc, out.decode(errors="replace")
        except asyncio.TimeoutError:
            logger.warning("git runner: %s timed out after %ss", " ".join(args), self._timeout)
            return 124, "git command timed out"
        except FileNotFoundError:
            return 127, "git not found"


async def ensure_working_clone(repo: str, state_dir: str, remote_url: str) -> str:
    """Ensure a working clone of ``repo`` exists under ``state_dir`` and return its path.

    One clone per repo, reused across ops (a fresh clone per merge would be pointless churn).
    Idempotent: clones if absent, else leaves it (the merger fetches per op). Bare-detached so
    a half-finished merge never leaves a dirty checked-out branch between ops.
    """
    path = os.path.join(state_dir, repo.replace("/", "__"))
    if os.path.isdir(os.path.join(path, ".git")):
        return path
    os.makedirs(state_dir, exist_ok=True)
    runner = SubprocessGitRunner(state_dir, timeout=300.0)
    rc, out = await runner.run("git", "clone", remote_url, path)
    if rc != 0:
        raise RuntimeError(f"git clone {repo} failed: {out.strip()[:200]}")
    return path
