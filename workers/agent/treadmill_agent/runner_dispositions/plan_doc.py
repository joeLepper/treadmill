"""``plan_doc`` disposition — like ``code`` but diff is confined to docs/plans/.

Per ADR-0022, the plan-doc role authors a plan doc at
``docs/plans/<date>-<slug>.md``. The handler first asserts that the
staged diff (if any) touches only paths under ``docs/plans/``; any
file outside that prefix is a constraint violation worth catching as
a step.failed rather than landing in a PR.

After the confinement check passes, the handler reuses the
code-disposition logic — same commit/push/PR flow.

Why a separate kind rather than a generic ``allowed_paths`` field on
``code``: Q22.a in ADR-0022. v0 ships ``plan_doc`` as a separate
kind so the constraint is enforceable; if a second path-restricted
role appears, ``plan_doc`` collapses into ``code`` with a per-role
``allowed_paths`` property. Deferred until that need arrives.
"""

from __future__ import annotations

import subprocess

from treadmill_agent import claude_code, git
from treadmill_agent.events import StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext
from treadmill_agent.runner_dispositions.code import handle as code_handle


_PLAN_DOC_PREFIX = "docs/plans/"


class PlanDocScopeError(claude_code.CodeAuthorError):
    """Raised when a plan_doc-kind step produces a diff that touches
    files outside ``docs/plans/``. Sub-class of ``CodeAuthorError`` so
    the existing runner failure path captures it without special
    handling — the operator sees the constraint violation in
    ``step.failed.error``.
    """


def _assert_diff_confined_to(repo_dir, prefix: str) -> None:
    """Raise ``PlanDocScopeError`` if any staged path is outside ``prefix``.

    Runs ``git diff --cached --name-only`` against the working tree
    after the runner has called ``stage_all``. An empty diff is
    handled separately (downstream code.handle raises the standard
    CodeAuthorError for "no changes to commit") — we don't try to
    rewrite that semantics here.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    )
    paths = [p for p in result.stdout.splitlines() if p.strip()]
    offending = [p for p in paths if not p.startswith(prefix)]
    if offending:
        raise PlanDocScopeError(
            f"plan_doc-kind step staged files outside {prefix!r}: "
            f"{sorted(offending)}; per ADR-0022 the plan-doc author "
            "must only touch files under that prefix."
        )


def handle(ctx: DispositionContext) -> StepOutput:
    """Stage everything, check confinement, hand off to code.handle."""
    # We must stage before we can inspect ``--cached`` paths. The
    # downstream ``code.handle`` re-stages but ``git add -A`` on an
    # already-clean index is a no-op, so this is safe.
    git.stage_all(ctx.repo_dir)
    _assert_diff_confined_to(ctx.repo_dir, _PLAN_DOC_PREFIX)
    return code_handle(ctx)
