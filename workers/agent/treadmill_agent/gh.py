"""GitHub CLI wrappers for non-git operations.

Today this is just ``pr_review`` (per ADR-0022) — the runner's review-
kind dispatch handler shells out to ``gh pr review`` to post the
verdict. The pattern matches ``git.open_pr``'s existing ``gh pr
create`` invocation: no ``GH_TOKEN`` in env (auth flows through the
``gh`` keyring populated at worker startup), no PAT on argv, stderr
captured for the operator on failure.

Why this lives outside ``git.py``: ``git.py`` wraps git plumbing. The
``gh pr review`` call doesn't touch git refs — it's a pure GitHub API
side effect — so it belongs alongside any future ``gh issue`` or ``gh
api`` wrappers, not inside the git module.

Per ADR-0022, the runner's review handler:

  1. Parses a ``VERDICT:`` marker from Claude Code's output.
  2. Calls ``pr_review(pr_number, verdict=..., body=summary)``.
  3. Returns a ``StepOutput`` with the verdict captured as an
     ``Artifact(kind="pr_review", ...)``.

This module deliberately keeps the wrapper tiny — one function, no
state — so the test suite can monkeypatch ``subprocess.run`` at the
boundary without faking complex object shapes.
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
