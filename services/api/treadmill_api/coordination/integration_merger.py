"""Integration git merge+push — ADR-0118 phase 2 (mechanical, moved off the coordinator prose).

In feature-branch mode (ADR-0110) a plan integrates each APPROVED task into a per-plan
integration branch ``joes-agents/<slug>`` cut from the plan's ``integration_base`` (ADR-0114,
``main`` by default). The coordinator agent did this in its own clone as template prose
(§9.3-feature); the router does it as deterministic, IDEMPOTENT code here.

Design (mirrors the ``dispatch_predicates``/``gate_position`` split):
* A pure ``MergeOp`` + ``integration_branch_for`` — the WHAT (branch/base), no side effects.
* ``integrate_task`` / ``drift`` — the git sequence, run through an injected ``GitRunner`` so
  the logic is foil-tested with a scripted runner and the real subprocess runner is prod-only.
* ATOMICITY by an ANCESTRY CHECK, not a lock. Before merging, we ask ``git merge-base
  --is-ancestor <task_head> origin/<integration_branch>``: if the task head is already in the
  branch — a duplicate approve, OR a re-run after a push that landed but whose process crashed
  before recording — we NO-OP. So a half-landed push is safe to retry; we never double-merge
  and never force-push (ADR-0110). A merge CONFLICT aborts and signals a conflict task; the
  router never resolves a conflict itself (that's a dispatched worker's judgment).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select

from treadmill_api.models.plan import Plan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeOp:
    repo: str
    task_head: str  # the approved task PR's head sha to integrate
    integration_branch: str  # joes-agents/<slug>
    base: str  # the plan's integration_base (resolved; 'main' by default)


def integration_branch_for(slug: str) -> str:
    """The per-plan integration branch (ADR-0110 falsifier: one branch per plan)."""
    return f"joes-agents/{slug}"


class GitRunner(Protocol):
    """A minimal git executor. Prod runs subprocess git in a working clone; tests inject a
    scripted runner. Returns (returncode, combined_output)."""

    async def run(self, *args: str) -> tuple[int, str]: ...


async def integrate_task(runner: GitRunner, op: MergeOp) -> str:
    """Idempotently merge ``op.task_head`` into ``op.integration_branch`` and push.

    Returns one of: ``merged`` (a new integration commit was pushed), ``already-integrated``
    (the head was already in the branch — a duplicate/retry no-op), ``conflict`` (a
    non-trivial merge, aborted; the caller opens a conflict task).
    """
    await runner.run("git", "fetch", "origin", op.integration_branch, op.task_head)
    # ATOMICITY: if the head is already in the integration branch, the merge already landed
    # (duplicate approve, or a retry after a half-recorded push) -> no-op, never double-merge.
    rc, _ = await runner.run(
        "git", "merge-base", "--is-ancestor", op.task_head, f"origin/{op.integration_branch}"
    )
    if rc == 0:
        logger.info(
            "integration: %s already in %s — no-op", op.task_head[:12], op.integration_branch
        )
        return "already-integrated"

    await runner.run(
        "git", "checkout", "-B", op.integration_branch, f"origin/{op.integration_branch}"
    )
    mrc, mout = await runner.run("git", "merge", "--no-ff", "--no-edit", op.task_head)
    if mrc != 0:
        # A conflict is a WORKER's judgment, never the router's. Abort cleanly; never
        # force-push, never let a half-merge sit on the branch.
        await runner.run("git", "merge", "--abort")
        logger.warning(
            "integration: merge conflict integrating %s into %s: %s",
            op.task_head[:12],
            op.integration_branch,
            (mout or "").strip()[:200],
        )
        return "conflict"
    await runner.run("git", "push", "origin", op.integration_branch)
    logger.info("integration: merged %s into %s", op.task_head[:12], op.integration_branch)
    return "merged"


async def drift(runner: GitRunner, integration_branch: str, base: str) -> str:
    """Merge ``origin/<base>`` into the integration branch on a base advance (ADR-0114).

    CRITICAL: drift against the SAME ref the branch was cut from. For a non-``main`` base, we
    must NEVER merge ``main`` — that repo's ``main`` may be one we never touch. Returns
    ``drifted`` | ``up-to-date`` | ``conflict``.
    """
    await runner.run("git", "fetch", "origin", base, integration_branch)
    rc, _ = await runner.run(
        "git", "merge-base", "--is-ancestor", f"origin/{base}", f"origin/{integration_branch}"
    )
    if rc == 0:
        return "up-to-date"
    await runner.run(
        "git", "checkout", "-B", integration_branch, f"origin/{integration_branch}"
    )
    mrc, _ = await runner.run("git", "merge", "--no-ff", "--no-edit", f"origin/{base}")
    if mrc != 0:
        await runner.run("git", "merge", "--abort")
        return "conflict"
    await runner.run("git", "push", "origin", integration_branch)
    return "drifted"


async def merge_op_for_plan(session: Any, plan_id: Any, slug: str, task_head: str) -> MergeOp:
    """Build the ``MergeOp`` from the plan row — the wrapper that reads ``integration_base``
    (ADR-0114; ``main`` when NULL) and the repo. Kept out of ``integrate_task`` so the git
    logic stays pure-runner and DB-free (the ``gate_weight_for_repo`` split)."""
    row = (
        await session.execute(
            select(Plan.repo, Plan.integration_base).where(Plan.id == plan_id)
        )
    ).one_or_none()
    if row is None:
        raise ValueError(f"no plan {plan_id!r}")
    repo, integration_base = row
    return MergeOp(
        repo=repo,
        task_head=task_head,
        integration_branch=integration_branch_for(slug),
        base=integration_base or "main",
    )
