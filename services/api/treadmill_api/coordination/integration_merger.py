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
    task_head: str  # the APPROVED task PR's head sha to integrate
    integration_branch: str  # joes-agents/<slug>
    base: str  # the plan's integration_base (resolved; 'main' by default)
    pr_number: int | None = None
    """The task's PR number. When set, ``integrate_task`` fetches ``refs/pull/<n>/head`` (a
    server may not have the bare ``task_head`` reachable — ADR-0119 host integrator) AND verifies
    the fetched tip still equals ``task_head``, refusing to integrate if the PR head moved since
    approval (the TOCTOU guard — never merge unapproved content as the operator, Bert #421)."""


def integration_branch_for(slug: str) -> str:
    """The per-plan integration branch (ADR-0110 falsifier: one branch per plan)."""
    return f"joes-agents/{slug}"


class GitRunner(Protocol):
    """A minimal git executor. Prod runs subprocess git in a working clone; tests inject a
    scripted runner. Returns (returncode, combined_output)."""

    async def run(self, *args: str) -> tuple[int, str]: ...


async def integrate_task(runner: GitRunner, op: MergeOp, *, max_retries: int = 3) -> str:
    """Idempotently merge ``op.task_head`` into ``op.integration_branch`` and push.

    Returns: ``merged`` (a new integration commit landed on origin), ``already-integrated``
    (the head was already in the branch — a duplicate/retry no-op), ``conflict`` (a real
    textual conflict, aborted; the caller opens a conflict task), ``merge-failed`` /
    ``fetch-failed`` (an INFRA error — missing object, dirty tree, network — the caller
    retries/escalates, NOT a conflict worker), ``push-rejected`` (origin kept advancing past
    ``max_retries`` — the caller re-drives), ``head-moved`` (``pr_number`` set and the PR head no
    longer equals the approved ``task_head`` — the head moved since approval; the caller escalates
    for re-eval and NEVER integrates the new, unapproved content — the ADR-0119 identity split's
    worst-case guard, Bert #421).

    ATOMICITY under concurrency (Bert's review): reconcile/retry + multi-replica mean another
    integrate/drift/human can advance ``origin/<branch>`` between our fetch and our push, making
    the push a NON-fast-forward that git rejects (we never force-push). So we CHECK the push rc
    and, on rejection, re-fetch + re-run the whole cycle in a bounded loop — SAFE because the
    ``--is-ancestor`` check is MONOTONIC: the branch is append-only (never force-pushed or
    rebased — the invariant this rests on), so a head merged via ``--no-ff`` stays an ancestor
    even as the branch advances, and a retry after a half-landed push correctly no-ops. We
    never return ``merged`` on a failed push.
    """
    for _ in range(max_retries + 1):
        if op.pr_number is not None:
            # Fetch the PR ref ALONE, so `rev-parse FETCH_HEAD` unambiguously names the PR tip —
            # a multi-ref fetch writes several FETCH_HEAD lines and rev-parse would resolve the
            # FIRST (the branch), verifying the wrong ref. Then verify the tip still equals the
            # approved head: a worker force-push between the host's queue read and this fetch
            # moves refs/pull/<n>/head, and merging its current tip blind would integrate
            # NEVER-APPROVED code as the operator — so we verify and refuse (Bert #421).
            frc, _ = await runner.run(
                "git", "fetch", "origin", f"refs/pull/{op.pr_number}/head"
            )
            if frc != 0:
                return "fetch-failed"
            rrc, tip = await runner.run("git", "rev-parse", "FETCH_HEAD")
            if rrc != 0:
                return "fetch-failed"
            if tip.strip() != op.task_head:
                logger.warning(
                    "integration: PR #%s head moved (%s != approved %s) — refusing to integrate",
                    op.pr_number, tip.strip()[:12], op.task_head[:12],
                )
                return "head-moved"
            # The approved sha is now local (via the PR ref); fetch the branch for the ancestry.
            frc, _ = await runner.run("git", "fetch", "origin", op.integration_branch)
            if frc != 0:
                return "fetch-failed"
        else:
            frc, _ = await runner.run(
                "git", "fetch", "origin", op.integration_branch, op.task_head
            )
            if frc != 0:
                return "fetch-failed"  # infra; stale/missing refs poison the ancestry check.
        rc, _ = await runner.run(
            "git", "merge-base", "--is-ancestor", op.task_head,
            f"origin/{op.integration_branch}",
        )
        if rc == 0:
            logger.info(
                "integration: %s already in %s — no-op",
                op.task_head[:12], op.integration_branch,
            )
            return "already-integrated"

        await runner.run(
            "git", "checkout", "-B", op.integration_branch, f"origin/{op.integration_branch}"
        )
        mrc, mout = await runner.run("git", "merge", "--no-ff", "--no-edit", op.task_head)
        if mrc != 0:
            # Distinguish a REAL conflict (unmerged files) from an INFRA failure (missing
            # object, dirty tree) — the former opens a worker conflict task, the latter must
            # not (Bert's non-blocking #1).
            _, unmerged = await runner.run("git", "ls-files", "-u")
            await runner.run("git", "merge", "--abort")
            if unmerged.strip():
                logger.warning(
                    "integration: real conflict integrating %s into %s",
                    op.task_head[:12], op.integration_branch,
                )
                return "conflict"
            logger.warning(
                "integration: merge failed (infra, not conflict) for %s: %s",
                op.task_head[:12], (mout or "").strip()[:200],
            )
            return "merge-failed"

        prc, _ = await runner.run("git", "push", "origin", op.integration_branch)
        if prc == 0:
            logger.info("integration: merged %s into %s", op.task_head[:12], op.integration_branch)
            return "merged"
        # Non-fast-forward: origin advanced under us. Re-fetch + re-run — the ancestry check
        # makes this safe (monotonic). Never force-push.
        logger.info(
            "integration: push rejected for %s (origin advanced) — retrying",
            op.integration_branch,
        )
    return "push-rejected"


async def drift(runner: GitRunner, integration_branch: str, base: str) -> str:
    """Merge ``origin/<base>`` into the integration branch on a base advance (ADR-0114).

    CRITICAL: drift against the SAME ref the branch was cut from. For a non-``main`` base, we
    must NEVER merge ``main`` — that repo's ``main`` may be one we never touch. Returns
    ``drifted`` | ``up-to-date`` | ``conflict``.
    """
    for _ in range(4):
        frc, _ = await runner.run("git", "fetch", "origin", base, integration_branch)
        if frc != 0:
            return "fetch-failed"
        rc, _ = await runner.run(
            "git", "merge-base", "--is-ancestor",
            f"origin/{base}", f"origin/{integration_branch}",
        )
        if rc == 0:
            return "up-to-date"
        await runner.run(
            "git", "checkout", "-B", integration_branch, f"origin/{integration_branch}"
        )
        mrc, _ = await runner.run("git", "merge", "--no-ff", "--no-edit", f"origin/{base}")
        if mrc != 0:
            _, unmerged = await runner.run("git", "ls-files", "-u")
            await runner.run("git", "merge", "--abort")
            return "conflict" if unmerged.strip() else "merge-failed"
        prc, _ = await runner.run("git", "push", "origin", integration_branch)
        if prc == 0:
            return "drifted"
        # push rejected — origin advanced; re-fetch + re-run (never force-push).
    return "push-rejected"


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
