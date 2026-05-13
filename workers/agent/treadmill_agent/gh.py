"""GitHub CLI wrappers for non-git operations.

Today this hosts ``pr_review`` (per ADR-0022) and ``pr_comment`` (per
task #108's same-author-block workaround) — the runner's review-kind
dispatch handler shells out to one of them to post the verdict.
The pattern matches ``git.open_pr``'s existing ``gh pr create``
invocation: no ``GH_TOKEN`` in env (auth flows through the ``gh``
keyring populated at worker startup), no PAT on argv, stderr captured
for the operator on failure.

Why this lives outside ``git.py``: ``git.py`` wraps git plumbing. The
``gh pr review`` / ``gh pr comment`` calls don't touch git refs —
they're pure GitHub API side effects — so they belong alongside any
future ``gh issue`` or ``gh api`` wrappers, not inside the git module.

Per ADR-0022 + task #108, the runner's review handler:

  1. Parses a ``VERDICT:`` marker from Claude Code's output.
  2. Calls ``pr_comment(pr_number, body=...)`` with the verdict as a
     prose header (path 1 of #108 — GitHub blocks same-author
     ``gh pr review``, so we post a plain comment instead). The
     mergeability VIEW reads ``decision`` from the Treadmill envelope,
     not from GitHub's review state, so the verdict still flows.
  3. Returns a ``StepOutput`` with the verdict captured as an
     ``Artifact(kind="pr_review", ...)``.

``pr_review`` is retained for callers that may want a formal review
later (e.g., once Treadmill becomes a GitHub App per task #109) and
for backwards compat with existing tests.

This module deliberately keeps the wrappers tiny — no state — so the
test suite can monkeypatch ``subprocess.run`` at the boundary without
faking complex object shapes.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Literal

logger = logging.getLogger("treadmill.agent.gh")


class GhCliError(RuntimeError):
    """Raised when the ``gh`` CLI exits non-zero. Stderr is captured in
    the message so the operator can debug from the worker logs alone."""


Verdict = Literal["approve", "request_changes", "comment"]


_VERDICT_FLAG: dict[str, str] = {
    "approve": "--approve",
    "request_changes": "--request-changes",
    "comment": "--comment",
}


def pr_review(
    pr_number: int,
    *,
    verdict: Verdict,
    body: str,
    cwd: Path | None = None,
) -> None:
    """Post a PR review via ``gh pr review``.

    Auth comes from the ``gh`` keyring populated at worker startup —
    we do **not** set ``GH_TOKEN`` in env (which would route the PAT
    through every child process's environment). When ``cwd`` is
    supplied the command runs there so ``gh`` discovers the upstream
    repo from the cloned ``origin``; ``cwd=None`` lets ``gh``
    auto-detect from the current process's working directory (handy in
    tests that don't materialize a working tree).

    Per ADR-0022's review-kind handler:

      * ``approve``         → ``--approve``         (PR is acceptable)
      * ``request_changes`` → ``--request-changes`` (blocks merge)
      * ``comment``         → ``--comment``         (observations only)

    Raises ``GhCliError`` on non-zero exit; the caller maps that to a
    ``step.failed`` event via the runner's existing exception handler.
    """
    if verdict not in _VERDICT_FLAG:
        raise GhCliError(
            f"unknown verdict {verdict!r}; expected one of "
            f"{sorted(_VERDICT_FLAG)}"
        )
    flag = _VERDICT_FLAG[verdict]
    cmd = ["gh", "pr", "review", str(pr_number), flag, "--body", body]
    logger.info("$ %s", " ".join(cmd[:5]))  # don't log the body verbatim
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GhCliError(
            f"gh pr review failed: exit {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def pr_comment(
    pr_number: int,
    *,
    body: str,
    cwd: Path | None = None,
) -> None:
    """Post a plain comment on a PR via ``gh pr comment``.

    Used by the review-kind disposition handler under task #108's path
    1: GitHub rejects ``gh pr review`` from the PR's own author, so
    Treadmill's self-reviews post a comment instead. The verdict lives
    in the body (as a prose header composed by the handler); the
    Treadmill envelope's ``decision`` field is what the mergeability
    VIEW reads, so the formal-review state on the PR page is no longer
    load-bearing.

    Auth comes from the ``gh`` keyring populated at worker startup —
    same posture as ``pr_review``. Raises ``GhCliError`` on non-zero
    exit; the caller maps that to a ``step.failed`` event via the
    runner's existing exception handler.
    """
    cmd = ["gh", "pr", "comment", str(pr_number), "--body", body]
    logger.info("$ %s", " ".join(cmd[:4]))  # don't log the body verbatim
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GhCliError(
            f"gh pr comment failed: exit {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
