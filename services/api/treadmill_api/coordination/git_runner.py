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

# The router creates integration MERGE commits (git merge --no-ff), which require a committer
# identity. A server clone has none by default ("Committer identity unknown"), so the runner
# supplies an identity via env on every call — no dependence on ambient git config.
#
# This DEFAULT is a fallback for environments with no real operator (CI, the container-side
# merger foils). The HOST integrator (ADR-0119) MUST override it with the OPERATOR's identity so
# integration commits are authored by the operator's gh user (attributed to them on GitHub), not
# this bot — otherwise the ADR-0119 falsifier fires (a joes-agents/* merge commit not authored by
# the operator). The host integrator resolves the operator identity from env and passes it in.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "treadmill-router",
    "GIT_AUTHOR_EMAIL": "router@treadmill.local",
    "GIT_COMMITTER_NAME": "treadmill-router",
    "GIT_COMMITTER_EMAIL": "router@treadmill.local",
}


def identity_env(name: str, email: str) -> dict[str, str]:
    """Build the GIT_AUTHOR/COMMITTER identity env for a given name+email (the operator, for the
    host integrator). Author AND committer are set so the merge commit attributes to them."""
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


class SubprocessGitRunner:
    """Runs ``git`` in a fixed working directory. Implements the ``GitRunner`` protocol
    (``run(*args) -> (rc, combined_output)``). stderr is folded into stdout so a caller sees
    the whole message (the merger's conflict-vs-infra check reads it).

    ``identity`` is the GIT_AUTHOR/COMMITTER env for the commits this runner makes; it defaults to
    the ``treadmill-router`` fallback (CI / container foils). The host integrator passes the
    OPERATOR's identity (ADR-0119) so merge commits attribute to the operator's gh user."""

    def __init__(
        self, cwd: str, *, timeout: float = 120.0, identity: dict[str, str] | None = None
    ) -> None:
        self._cwd = cwd
        self._timeout = timeout
        self._identity = identity if identity is not None else _GIT_IDENTITY

    async def run(self, *args: str) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self._cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, **self._identity},
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
