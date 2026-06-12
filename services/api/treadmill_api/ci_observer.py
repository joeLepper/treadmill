"""API-side CI-observer — ONE ``task.ci_result`` per completed check suite.

ADR-0090: the coordinator (and evaluator) woke on every
``github.check_run.completed`` — ~13 wakes per PR whose only decision
lives in the LAST one. The API is the always-on control plane (ADR-0091:
it never pauses with a team), so the rollup is computed here at ingest:
when a check_run delivery's embedded suite snapshot reads ``completed``,
emit ONE ``task.ci_result`` carrying GitHub's own suite conclusion.

Attribution (two layers, decided in task 5dd4a32d):

* ``resolve_task_by_head_sha`` (#335) — the fast path, fed by the
  ingest-time ``task_prs.head_sha`` writer this task added (pr_opened /
  pr_synchronize updates).
* Events-join fallback — the writer can lose the pr_opened race (the
  coordinator registers ``task_prs`` seconds AFTER the webhook lands),
  so an unresolved head falls back to the join the mergeability VIEW
  uses: latest pr_opened/pr_synchronize event carrying (repo, head_sha)
  → pr_number → ``task_prs`` row.

CLOSED-PR DECISION (task 5dd4a32d, from the #335 review note): ci_result
IS emitted for heads whose newest ``task_prs`` row is closed. Rationale:
a suppressed signal costs more than a redundant event — reruns during
the merge window must land, and the coordinator already ignores events
for terminal tasks. Consumers that want closed-PR silence filter on
their side.

Suites that never complete (netlify parks in ``queued`` on this repo —
2026-06-12) never emit: completion is keyed strictly on the embedded
suite snapshot reaching ``completed`` with a conclusion.

Idempotency: at most one ci_result per (head_sha, check_suite_id,
conclusion) — simultaneous final deliveries collapse; a RERUN that
changes the suite conclusion emits anew (the coordinator needs the
changed verdict); a rerun reproducing the same conclusion is
informationally idempotent and stays suppressed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.events import TaskCiResult, encode_payload
from treadmill_api.models import Event, TaskPR
from treadmill_api.resolvers import resolve_task_by_head_sha

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SuiteCompletion:
    """A check-run delivery that completes its suite."""

    repo: str
    head_sha: str
    check_suite_id: int
    conclusion: str
    app_slug: str
    pr_number: int | None


def suite_completion_from_payload(payload: dict[str, Any]) -> SuiteCompletion | None:
    """Pure detection: the normalized check_run payload's embedded suite
    snapshot, iff it reads ``completed`` with a conclusion. Anything
    else (suite still running, netlify's eternal ``queued``, legacy rows
    without the snapshot) is None."""
    if payload.get("suite_status") != "completed":
        return None
    conclusion = payload.get("suite_conclusion")
    suite_id = payload.get("check_suite_id")
    if not conclusion or suite_id is None:
        return None
    return SuiteCompletion(
        repo=payload.get("repo") or "",
        head_sha=payload.get("head_sha") or "",
        check_suite_id=int(suite_id),
        conclusion=str(conclusion),
        app_slug=payload.get("app_slug") or "",
        pr_number=payload.get("pr_number"),
    )


async def _fallback_task_id_via_events(
    session: AsyncSession, repo: str, head_sha: str,
) -> Any | None:
    """The events-join fallback (documented in #335): latest
    pr_opened/pr_synchronize carrying (repo, head_sha) → pr_number →
    task_prs. Covers heads the ingest-time writer missed (the pr_opened
    vs task_prs-registration race)."""
    from sqlalchemy import func

    row = await session.execute(
        select(Event.payload["pr_number"].astext)
        .where(
            Event.entity_type == "github",
            Event.action.in_(("pr_opened", "pr_synchronize")),
            Event.payload["repo"].astext == repo,
            Event.payload["head_sha"].astext == head_sha,
        )
        .order_by(Event.created_at.desc())
        .limit(1)
    )
    pr_number_text = row.scalar_one_or_none()
    if pr_number_text is None:
        return None
    task_row = await session.execute(
        select(TaskPR.task_id).where(
            func.lower(TaskPR.repo) == repo.lower(),
            TaskPR.pr_number == int(pr_number_text),
        )
    )
    return task_row.scalar_one_or_none()


async def maybe_emit_ci_result(
    session: AsyncSession,
    publisher: Any,
    payload: dict[str, Any],
) -> Event | None:
    """Emit ONE ``task.ci_result`` if this check_run delivery completes
    its suite. Returns the persisted Event, or None (not a completion /
    unattributable / already emitted). Never raises into the webhook
    ingest — a failed rollup must not 500 the ingress."""
    try:
        completion = suite_completion_from_payload(payload)
        if completion is None:
            return None

        # Idempotency: one ci_result per (head, suite, conclusion).
        existing = await session.execute(
            select(Event.id)
            .where(
                Event.entity_type == "task",
                Event.action == "ci_result",
                Event.commit_sha == completion.head_sha,
                Event.payload["check_suite_id"].astext
                == str(completion.check_suite_id),
                Event.payload["conclusion"].astext == completion.conclusion,
            )
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return None

        task = await resolve_task_by_head_sha(
            session, completion.repo, completion.head_sha,
        )
        task_id = task.id if task is not None else None
        if task_id is None:
            task_id = await _fallback_task_id_via_events(
                session, completion.repo, completion.head_sha,
            )
        if task_id is None:
            logger.debug(
                "ci_result: suite %s at %s unattributable (no task_pr) — skipping",
                completion.check_suite_id, completion.head_sha[:8],
            )
            return None

        typed = TaskCiResult(
            repo=completion.repo,
            pr_number=completion.pr_number,
            head_sha=completion.head_sha,
            check_suite_id=completion.check_suite_id,
            conclusion=completion.conclusion,
            app_slug=completion.app_slug,
        )
        event = Event(
            entity_type="task",
            action="ci_result",
            task_id=task_id,
            payload=encode_payload(typed),
            commit_sha=completion.head_sha,
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        try:
            await publisher.publish(event, typed)
        except Exception:
            logger.exception(
                "ci_result publish failed; Event row %s persisted "
                "(consumers reconcile from the table)", event.id,
            )
        logger.info(
            "ci_result: suite %s (%s) at %s -> %s, task %s",
            completion.check_suite_id, completion.app_slug,
            completion.head_sha[:8], completion.conclusion, task_id,
        )
        return event
    except Exception:
        logger.exception("ci-observer failed; ingest continues (event row intact)")
        return None
