"""Cross-entity resolvers — lookups that bridge GitHub-keyed facts to tasks.

First resident: ``resolve_task_by_head_sha`` (task ec0e534c), the
ADR-0063-deferred ``(repo, head_sha)`` lookup. ADR-0090's CI-observer is
its first consumer: a completed check SUITE arrives keyed by commit SHA,
and the observer needs the task whose PR carries that head so it can
emit ``task.ci_result``.

Resolution contract: the task of the MOST-RECENT ``task_prs`` row
matching ``(repo, head_sha)`` (``created_at`` descending), else ``None``.
Most-recent matters because a superseded task's PR can be reopened or a
follow-up task can push the same head (cherry-pick, retry-branch): the
newest registration is the one the coordinator is currently driving.

CLOSED PRs are deliberately NOT filtered (reviewer design note,
PR #335): the query ignores ``closed_at``, so a newest-but-closed
registration beats an older still-open one. The CI-observer (first
consumer) must choose: if a check suite completing on a closed PR's
head should not emit ``task.ci_result``, filter on the caller's side or
extend this resolver deliberately — silence here is a decision, not an
oversight.

``task_prs.head_sha`` is nullable and only as fresh as its writers —
rows with a NULL or stale head simply never match. Callers that need
attribution for heads no writer recorded can fall back to the events
join the mergeability VIEW uses (``github.pr_opened``/``pr_synchronize``
payloads carry repo + pr_number + head_sha); that fallback is
deliberately NOT folded in here — it changes the freshness and
trust story and belongs to the consumer that needs it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models import Task, TaskPR


async def resolve_task_by_head_sha(
    session: AsyncSession, repo: str, head_sha: str,
) -> Task | None:
    """Task for the most-recent ``task_prs`` row matching (repo, head_sha).

    Returns ``None`` when no row matches — the caller decides whether an
    unattributable SHA is ignorable (a PR from outside the team) or worth
    a fallback lookup.
    """
    result = await session.execute(
        select(Task)
        .join(TaskPR, TaskPR.task_id == Task.id)
        .where(TaskPR.repo == repo, TaskPR.head_sha == head_sha)
        # Tiebreaker (PR #335 review): created_at alone is nondeterministic
        # for same-transaction registrations (the coordinator-backfill
        # shape — NOW() is transaction-stable). task_prs has NO id column
        # (composite PK repo+pr_number), so pr_number is the stability
        # key: unique within the repo filter, and a higher number is the
        # later GitHub-side registration. Stability first, recency proxy
        # second.
        .order_by(TaskPR.created_at.desc(), TaskPR.pr_number.desc())
        .limit(1)
    )
    return result.scalars().first()
