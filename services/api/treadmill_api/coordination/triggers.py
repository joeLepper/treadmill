"""Event trigger evaluator — fires workflows on github events.

Per ADR-0007 §"Event triggers" + Week-3 plan success criterion 5, the
``event_triggers`` table maps ``(repo, event_type) → workflow_id`` rows.
This module reads that table on relevant github events from the
coordination consumer and dispatches the resulting workflow against the
event's task.

Per ADR-0015 §"Open questions Q15.b", the cap policy lives here in the
evaluator (not in the workflow definition). Two workflows are capped:

  * ``wf-ci-fix``:   3 attempts per task before the evaluator skips
                     dispatch and emits a ``task.capped`` log line.
  * ``wf-conflict``: 3 attempts per task before the same cap kicks in.

Other workflows have no cap — ``wf-review`` and ``wf-validate`` re-fire
on every ``pr_synchronize`` by design (per ADR-0013 the new HEAD
invalidates prior thumbs).

Per-event filters
-----------------

The table-level mapping says "this event fires this workflow"; the
filters here say "only this *flavor* of the event should fire". Bunkhouse
precedent (``events/triggers.py:TriggerEvaluator``) uses the same split.

  * ``pr_review_submitted``: only ``state='changes_requested'`` fires
    ``wf-feedback`` (an ``approved`` review is the human signing off, not
    a problem to solve).
  * ``check_run_completed``: only ``conclusion='failure'`` (and the few
    cousins: ``timed_out``, ``action_required``, ``startup_failure``)
    fires ``wf-ci-fix``. Greens / neutrals / cancels are intentionally
    excluded — they don't indicate broken code.

The ``pr_synchronize`` fan-out
------------------------------

Per the Week-3 plan §C.2, ``pr_synchronize`` fires *both* ``wf-review``
and ``wf-validate`` concurrently. The ``event_triggers`` table's
``(repo, event_type)`` unique constraint allows only one row per pair,
so the evaluator hardcodes the second workflow in the fan-out: the row
in the table dispatches ``wf-review``; the evaluator additionally
dispatches ``wf-validate`` for ``pr_synchronize`` events. Documented
here so the divergence between the table and the runtime is visible to
the reader.

Idempotency
-----------

The trigger evaluator creates a new ``WorkflowRun`` per fire, by design:
each ``pr_synchronize`` *should* produce a fresh ``wf-review`` /
``wf-validate`` pair against the new HEAD. The dispatcher's existing
idempotency probe (``_has_step_ready_event`` short-circuit on a prior
``step.ready`` for the task) would prevent that — so the evaluator does
**not** call ``Dispatcher.dispatch_task``. It constructs the run + steps
directly + publishes ``step.ready`` for step 1, mirroring bunkhouse's
``TriggerEvaluator._create_workflow_run``.

Bunkhouse precedent
-------------------

Cribbed from ``bunkhouse/services/api/bunkhouse/events/triggers.py``:

  * Repo-specific rows override catch-all rows (``repo IS NULL``).
  * Cap-policy is per (task, workflow) attempt count.
  * Failing CI conclusions form a frozenset; only those fire ``wf-ci-fix``.
  * Run creation is direct (no shared call into ``dispatch_task``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.dialects.postgresql import insert as pg_insert

from treadmill_api.coordination.dispatch_dedup import maybe_dispatch_with_dedup
from treadmill_api.observability import inject_trace_context
from treadmill_api.events.step import StepCompleted, StepReady
from treadmill_api.events.schedule import ScheduledTick
from treadmill_api.models import (
    Event,
    EventTrigger,
    Schedule,
    Task,
    TaskPR,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
    WorkflowVersionStep,
)

logger = logging.getLogger("treadmill.coordination.triggers")


# Cap constants per ADR-0015 Q15.b. The bunkhouse precedent caps
# ``wf-ci-fix`` and ``wf-validation-fix`` at 3 each; Treadmill's
# equivalent is ``wf-ci-fix`` + ``wf-conflict``. Per ADR-0029 Q29.e,
# ``wf-feedback`` is capped at 5 attempts per task (across all trigger sources).
CI_FIX_WORKFLOW_ID = "wf-ci-fix"
CONFLICT_WORKFLOW_ID = "wf-conflict"
FEEDBACK_WORKFLOW_ID = "wf-feedback"
DOC_AMEND_WORKFLOW_ID = "wf-doc-amend"
ARCHITECTURE_RESOLVE_WORKFLOW_ID = "wf-architecture-resolve"
CI_FIX_MAX_ATTEMPTS = 3
CONFLICT_RESOLVE_MAX_ATTEMPTS = 3
FEEDBACK_MAX_ATTEMPTS = 5
DOC_AMEND_MAX_ATTEMPTS = 5
ARCHITECTURE_RESOLVE_MAX_ATTEMPTS = 5

# The check_id that routes wf-validate failures to wf-doc-amend instead
# of wf-feedback. Any other check failure still dispatches wf-feedback.
DOCS_CURRENT_CHECK_ID = "docs-current-with-pr"


# Conclusions that represent a genuine CI failure — the only ones that
# should fire the CI-fix workflow. Mirrors bunkhouse's
# ``FAILURE_CONCLUSIONS`` frozenset; ``success``, ``neutral``,
# ``cancelled``, ``skipped``, and ``stale`` are intentionally excluded.
FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure"}
)


# Per-event extra fan-out: ``pr_opened`` and ``pr_synchronize`` both fire
# ``wf-review`` (via the event_triggers row) AND ``wf-validate``. The
# event_triggers table only supports one workflow per (repo, event_type),
# so the second workflow is named here. Symmetric across the two PR
# verbs because auto-merge (ADR-0031) requires ``validate_decision='pass'``
# and that VIEW projection only sees runs that actually executed — so a
# first-cycle PR with no pr_synchronize was structurally unable to
# auto-merge before 2026-05-15 (surfaced by the first end-to-end smoke).
_EXTRA_FANOUT_WORKFLOWS: dict[str, list[str]] = {
    "pr_opened": ["wf-validate"],
    "pr_synchronize": ["wf-validate"],
}


async def evaluate_triggers(
    session: AsyncSession,
    dispatcher: Any,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> list[uuid.UUID]:
    """Look up event_triggers matching the event; dispatch each matched
    workflow against the task. Returns the run_ids of dispatched runs.

    ``event_type`` is the bare verb (``pr_opened``, ``pr_synchronize``,
    ``pr_review_submitted``, ``check_run_completed``, ``pr_conflict``) —
    mirrors the ``event_triggers.event_type`` column shape. The caller
    (the coordination consumer's github branch) passes
    ``record["action"]`` as ``event_type``.

    ``dispatcher`` is the consumer's ``Dispatcher`` — used here only for
    its publisher + SQS client. We do **not** call ``dispatch_task``;
    see the module docstring.

    Idempotency: each trigger-fire creates a fresh ``WorkflowRun`` by
    design (an ``pr_synchronize`` *should* re-fire review). Re-delivery
    of the same github event would create duplicate runs — that's a
    bigger problem the webhook receiver has separately handled (the
    Event row INSERT is idempotent on ``event_id``); by the time the
    consumer sees the event the second time, the event is already in
    the DB but the trigger evaluator runs again. For v0 we accept that
    edge case (rare in practice — SQS delivery is at-least-once but the
    consumer ack-deletes after handle, so dup events are unusual). A
    follow-up could dedupe by per-event-id record in ``workflow_runs``.
    """
    # ── Per-event-type guard: drop events we know aren't actionable ──────
    if not _event_passes_filter(event_type, payload):
        return []

    # ── Resolve the task this event belongs to ───────────────────────────
    # Github events flow through the webhook receiver, which looks up
    # task_id via ``task_prs (repo, pr_number)`` and stamps it on the
    # Event row. The consumer hands us the raw record payload; we
    # resolve the task again here so we have the typed Task object the
    # rest of the evaluator wants.
    repo = payload.get("repo")
    pr_number = payload.get("pr_number")
    if not repo or pr_number is None:
        # No PR context — can't resolve a task. Some events (e.g.
        # ``check_run_completed`` on a non-PR check) have no PR number;
        # those don't have a task to fire against, so drop them.
        logger.debug(
            "trigger evaluator: skipping %s — no repo+pr_number in payload",
            event_type,
        )
        return []

    task = await _resolve_task_by_pr(session, repo, pr_number)
    if task is None:
        # No task owns this PR. Per ADR-0007 §"Cache-then-heal", these
        # events are buffered in Redis by the webhook receiver and
        # replayed once the ``task_prs`` bridge populates. By the time
        # the replay hits us, ``task`` will resolve.
        logger.debug(
            "trigger evaluator: skipping %s — no task_prs entry for %s/PR#%s",
            event_type, repo, pr_number,
        )
        return []

    # ── Gather candidate triggers ────────────────────────────────────────
    # Repo-specific rows take precedence over catch-alls (per ADR-0007
    # §"GitHub webhook ingestion" and bunkhouse precedent). We collect
    # candidates by ``workflow_id`` keyed dedup: if a repo-specific row
    # exists, the catch-all row for the same event_type is filtered out.
    candidate_workflow_ids = await _resolve_candidate_workflows(
        session, repo=repo, event_type=event_type,
    )

    # Append any hardcoded fan-out workflows (e.g. wf-validate on
    # pr_synchronize). De-dup against the table-driven set.
    for extra_wf in _EXTRA_FANOUT_WORKFLOWS.get(event_type, []):
        if extra_wf not in candidate_workflow_ids:
            candidate_workflow_ids.append(extra_wf)

    if not candidate_workflow_ids:
        return []

    # ── Dispatch each candidate ──────────────────────────────────────────
    # Each dispatch is gated by ADR-0026's dedup table: workflows with a
    # deterministic dedup key (wf-review, wf-feedback, wf-ci-fix,
    # wf-conflict) skip the second-and-onward dispatch on identical
    # content. Workflows that opt out (wf-author, wf-plan) or events
    # missing the discriminator field fall through to unconditional
    # dispatch via the helper's None-key short-circuit.
    created_run_ids: list[uuid.UUID] = []
    for workflow_id in candidate_workflow_ids:
        # Cap policy gate. Capped workflows that hit the limit log a
        # warning + return None instead of dispatching.
        if await _is_capped(session, task.id, workflow_id):
            logger.warning(
                "trigger evaluator: %s capped for task %s (>=%d prior runs); "
                "skipping dispatch for %s",
                workflow_id, task.id, _cap_for(workflow_id), event_type,
            )
            continue

        # Bind loop variables to avoid the late-binding closure pitfall.
        _workflow_id = workflow_id

        async def _dispatch() -> uuid.UUID | None:
            return await _create_and_publish_run(
                session,
                dispatcher,
                task=task,
                workflow_id=_workflow_id,
                trigger=f"webhook:{event_type}",
            )

        run_id = await maybe_dispatch_with_dedup(
            session,
            workflow_id=workflow_id,
            payload=payload,
            dispatch_fn=_dispatch,
        )
        if run_id is not None:
            created_run_ids.append(run_id)

    return created_run_ids


def _event_passes_filter(event_type: str, payload: dict[str, Any]) -> bool:
    """Apply per-event-type filters. Returns ``True`` if the event
    should fire its workflow(s); ``False`` if it should be dropped.

    Bunkhouse precedent (``triggers.py:TriggerEvaluator.evaluate``):

      * ``check_run_completed``: only ``conclusion`` in FAILURE_CONCLUSIONS
        fires ``wf-ci-fix``.
      * ``pr_review_submitted``: only ``state='changes_requested'`` fires
        ``wf-feedback``. Approvals and plain comments are not actionable.
    """
    if event_type == "check_run_completed":
        conclusion = payload.get("conclusion")
        if conclusion not in FAILURE_CONCLUSIONS:
            logger.debug(
                "trigger evaluator: %s conclusion=%r is not a CI failure; skipping",
                event_type, conclusion,
            )
            return False
    elif event_type == "pr_review_submitted":
        state = payload.get("state")
        if state != "changes_requested":
            logger.debug(
                "trigger evaluator: %s state=%r is not changes_requested; skipping",
                event_type, state,
            )
            return False
    return True


async def _resolve_task_by_pr(
    session: AsyncSession, repo: str, pr_number: int,
) -> Task | None:
    """Resolve the Task for a (repo, pr_number) pair via the ``task_prs``
    bridge. Returns ``None`` if no row exists yet (the webhook receiver
    will have buffered the event in Redis per ADR-0007's cache-then-heal
    pattern; this branch is reached only when the consumer processes a
    replayed event after the bridge populates)."""
    result = await session.execute(
        select(Task)
        .join(TaskPR, TaskPR.task_id == Task.id)
        .where(
            func.lower(TaskPR.repo) == repo.lower(),
            TaskPR.pr_number == int(pr_number),
        )
    )
    return result.scalar_one_or_none()


async def _resolve_candidate_workflows(
    session: AsyncSession, *, repo: str, event_type: str,
) -> list[str]:
    """Return the workflow ids that match ``(repo, event_type)`` in
    ``event_triggers``, with repo-specific rows overriding catch-alls.

    Bunkhouse pattern: a repo-specific row for the (repo, event_type)
    pair takes precedence over the catch-all row. We implement this by
    fetching every enabled row for ``event_type`` (both ``repo=NULL``
    and ``repo=<the repo>``) and then collapsing: if a repo-specific
    row exists, drop the catch-all from the result.
    """
    result = await session.execute(
        select(EventTrigger.workflow_id, EventTrigger.repo)
        .join(Workflow, Workflow.id == EventTrigger.workflow_id)
        .where(
            EventTrigger.event_type == event_type,
            EventTrigger.enabled.is_(True),
        )
        .where(
            (EventTrigger.repo.is_(None))
            | (func.lower(EventTrigger.repo) == repo.lower())
        )
    )
    rows = result.all()
    repo_specific = [row.workflow_id for row in rows if row.repo is not None]
    if repo_specific:
        # Repo-specific rows win; catch-all rows are masked entirely
        # (regardless of which workflow they map to).
        return repo_specific
    return [row.workflow_id for row in rows if row.repo is None]


def _cap_for(workflow_id: str) -> int:
    """Return the attempt cap for a workflow, or ``0`` if uncapped."""
    if workflow_id == CI_FIX_WORKFLOW_ID:
        return CI_FIX_MAX_ATTEMPTS
    if workflow_id == CONFLICT_WORKFLOW_ID:
        return CONFLICT_RESOLVE_MAX_ATTEMPTS
    if workflow_id == FEEDBACK_WORKFLOW_ID:
        return FEEDBACK_MAX_ATTEMPTS
    if workflow_id == DOC_AMEND_WORKFLOW_ID:
        return DOC_AMEND_MAX_ATTEMPTS
    return 0


async def _is_capped(
    session: AsyncSession, task_id: uuid.UUID, workflow_id: str,
) -> bool:
    """Return ``True`` if the per-task attempt count for this workflow
    is at or above its cap. ``False`` for workflows with no cap.

    The count is over ``workflow_runs`` joined to ``workflow_versions``
    on ``workflow_id`` (the slug). One WorkflowRun = one attempt, by
    bunkhouse convention.
    """
    cap = _cap_for(workflow_id)
    if cap == 0:
        return False
    result = await session.execute(
        select(func.count(WorkflowRun.id))
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
        .where(
            WorkflowRun.task_id == task_id,
            WorkflowVersion.workflow_id == workflow_id,
        )
    )
    count = result.scalar_one() or 0
    return count >= cap


async def maybe_dispatch_feedback_on_terminal_failure(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
    workflow_id: str,
    fail_decision: str,
) -> uuid.UUID | None:
    """Fire ``wf-feedback`` when a step completes with a terminal verdict
    that should trigger analysis + resolution.

    Handles four scenarios:
      1. ``wf-review`` with ``decision='changes_requested'`` (task #108 path 1)
      2. ``wf-validate`` with ``decision='fail'`` (ADR-0029)
      3. ``wf-validate`` with ``decision='error'`` (ADR-0029)
      4. ``wf-author`` with ``decision='fail'`` (ADR-0037)

    Companion to the github-webhook-driven ``evaluate_triggers``: that
    function fires ``wf-feedback`` on a ``pr_review_submitted`` event
    with ``state='changes_requested'`` (human reviewer outside Treadmill).
    This function fires it on Treadmill's own verdicts (self-review via
    ``wf-review`` which no longer emits ``pr_review_submitted`` webhook,
    validation failures via ``wf-validate``, and author failures via
    ``wf-author``).

    Skips cleanly when:
      * The completed step's workflow doesn't match ``workflow_id``.
      * The envelope's ``decision`` isn't ``fail_decision``.
      * Owning task can't be resolved (deleted between dispatch + completion).
      * No ``WorkflowVersion`` exists for ``wf-feedback`` (un-seeded install).
      * Task has already dispatched wf-feedback >= 5 times (cap per ADR-0029 Q29.e).

    Dedup is gated by ADR-0026's ``WorkflowDispatchDedup`` table keyed on:
      * ``wf-feedback:<repo>:review-run=<wf_review_run_id>`` for wf-review
      * ``wf-feedback:<repo>:validate-run=<wf_validate_run_id>`` for wf-validate
      * ``wf-feedback:<repo>:author-fail-run=<wf_author_run_id>`` for wf-author

    Different namespaces prevent different trigger sources from colliding
    on the dedup table — multiple legitimate sources can fire feedback
    against the same task.

    Returns the new ``wf-feedback`` run's id, or ``None`` if any skip
    condition fired.
    """
    if typed.output.decision != fail_decision:
        return None

    # Resolve (resolved_workflow_id, run_id, task_id, repo) for this step.
    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.id.label("run_id"),
            WorkflowRun.task_id,
            Task.repo,
        )
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .join(
            WorkflowVersion,
            WorkflowVersion.id == WorkflowRun.workflow_version_id,
        )
        .join(Task, Task.id == WorkflowRun.task_id)
        .where(WorkflowRunStep.id == step_id)
    )
    row = result.first()
    if row is None:
        logger.debug(
            "feedback trigger: no run/task resolvable for step %s; skipping",
            step_id,
        )
        return None
    if row.workflow_id != workflow_id:
        # Only the specified workflow's verdict fires feedback.
        return None

    # Check cap: if this task has already dispatched wf-feedback >= FEEDBACK_MAX_ATTEMPTS,
    # skip and log task.capped.
    if await _is_capped(session, row.task_id, FEEDBACK_WORKFLOW_ID):
        logger.warning(
            "feedback trigger: %s capped for task %s (>=%d prior runs); skipping",
            FEEDBACK_WORKFLOW_ID, row.task_id, FEEDBACK_MAX_ATTEMPTS,
        )
        return None

    # Re-fetch the Task so the dispatch helper has the full ORM object.
    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    # Build the payload with the appropriate dedup namespace.
    if workflow_id == "wf-review":
        payload = {"repo": row.repo, "review_run_id": str(row.run_id)}
        trigger = "self:wf-review-changes-requested"
    elif workflow_id == "wf-validate":
        payload = {"repo": row.repo, "validate_run_id": str(row.run_id)}
        trigger = f"self:wf-validate-{fail_decision}"
    elif workflow_id == "wf-author":
        payload = {"repo": row.repo, "author_run_id": str(row.run_id)}
        trigger = "self:wf-author-fail"
    else:
        logger.warning(
            "feedback trigger: unexpected workflow_id %s for step %s; skipping",
            workflow_id, step_id,
        )
        return None

    async def _dispatch() -> uuid.UUID | None:
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id="wf-feedback",
            trigger=trigger,
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id="wf-feedback",
        payload=payload,
        dispatch_fn=_dispatch,
    )


async def maybe_dispatch_feedback_on_review_changes_requested(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """Legacy wrapper for backwards compatibility with wf-review feedback.

    Delegates to the generalized
    ``maybe_dispatch_feedback_on_terminal_failure`` with the
    wf-review workflow and changes_requested decision.
    """
    return await maybe_dispatch_feedback_on_terminal_failure(
        session,
        dispatcher,
        step_id=step_id,
        typed=typed,
        workflow_id="wf-review",
        fail_decision="changes_requested",
    )


async def maybe_dispatch_arbitration_on_deadlock(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """Dispatch ``wf-architecture-resolve`` when a ralph-loop deadlock fires
    (ADR-0038, twice-widened 2026-05-15).

    The deadlock signal is: a ``wf-feedback`` step.completed with
    ``decision`` in ``{'responded-without-change', 'fail'}`` (second
    widening — see below) against a task whose most-recent gate-bearing
    workflow returned a blocking verdict:

      * ``wf-review.decision == 'changes_requested'`` — original
        ADR-0038 predicate; reviewer disagrees with author + feedback
        declines to act.
      * ``wf-validate.decision == 'fail'`` — added 2026-05-15 after
        observing task ``c5438ed1`` hit functionally the same shape
        (validator said work was incomplete, reviewer said approved,
        feedback declined to author the missing pieces).

    Either way the feedback role examined the gate's rationale and
    declined to author any change while a load-bearing gate still
    blocks merge. Two or more LLM roles disagree about the same diff;
    no role has authority over the other.

    Architect's verdict (per ADR-0032 ``ArchitectVerdict``) drives the
    next move:

      * ``accept-as-is`` → architect disposition emits a
        ``review.override`` event; mergeability VIEW (post-0016
        migration) projects ``review_decision='approved'``; auto-merge
        proceeds. **Limitation:** today's override mechanism covers
        only the review axis. For a validate-fail-driven deadlock the
        architect's ``accept-as-is`` won't unblock the merge — the
        architect should use ``amend`` (route to wf-plan to author the
        missing pieces) instead. A future ADR can add a
        ``validate.override`` mirror.
      * ``amend`` → dispatcher routes to ``wf-plan`` (handled elsewhere).
      * ``supersede`` → dispatcher routes to ``wf-doc-amend`` (handled
        elsewhere).
      * ``uncertain`` → surfaced to operator via the cap below.

    Skips cleanly when:
      * The completed step isn't ``wf-feedback`` or its decision isn't
        in the widened {``responded-without-change``, ``fail``} set.
      * Neither blocking gate signal is present on the most-recent run
        (no deadlock).
      * The task has already dispatched
        ``wf-architecture-resolve`` >= 5 times (cap per ADR-0029 Q29.e /
        ADR-0038).

    Dedup key per ADR-0026:
        ``wf-architecture-resolve:<repo>:deadlock-feedback-run=<wf_feedback_run_id>``

    Distinct from the existing
    ``wf-architecture-resolve:<repo>:class-c-learning=<learning_slug>``
    used for ADR-0032's Class C learnings — different trigger sources,
    different namespaces.
    """
    # Widened 2026-05-15 (second widening): also fire on `fail`. Observed
    # tasks 472e3ddc, 2a3eaadb, b25b3f5d (Plans A + B downstream of OTel
    # SDK / crystallization-disposition) all dead-ended at
    # wf-feedback.decision='fail' with no further auto-dispatch. ADR-0037
    # fires wf-feedback on wf-author-fail; nothing fires on
    # wf-feedback-fail. Architect arbitration is the right next move
    # whether the feedback role declined to act
    # (``responded-without-change``) or tried and gave up (``fail``).
    if typed.output.decision not in ("responded-without-change", "fail"):
        return None

    # Resolve workflow + run + task + repo for the completing step.
    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.id.label("run_id"),
            WorkflowRun.task_id,
            Task.repo,
        )
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .join(
            WorkflowVersion,
            WorkflowVersion.id == WorkflowRun.workflow_version_id,
        )
        .join(Task, Task.id == WorkflowRun.task_id)
        .where(WorkflowRunStep.id == step_id)
    )
    row = result.first()
    if row is None or row.workflow_id != FEEDBACK_WORKFLOW_ID:
        return None

    # Look up the most-recent terminal-blocking signal for this task on
    # EITHER gate-bearing workflow. Two-axis predicate per the 2026-05-15
    # widening (the original ADR-0038 was review-centric; we observed
    # c5438ed1 hit the same disagreement shape with the *validator*
    # saying ``fail`` while the reviewer said ``approved`` and feedback
    # declined to act — functionally identical deadlock, missed by the
    # review-only predicate):
    #
    #   * latest ``wf-review.step.completed.output.decision == 'changes_requested'``
    #   * OR latest ``wf-validate.step.completed.output.decision == 'fail'``
    #
    # Either is grounds to dispatch the architect for arbitration.
    blocking_signal: str | None = None
    for gate_workflow_id, blocking_decision in (
        ("wf-review", "changes_requested"),
        ("wf-validate", "fail"),
    ):
        gate_result = await session.execute(
            select(WorkflowRunStep.output)
            .join(WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id)
            .join(
                WorkflowVersion,
                WorkflowVersion.id == WorkflowRun.workflow_version_id,
            )
            .where(
                WorkflowRun.task_id == row.task_id,
                WorkflowVersion.workflow_id == gate_workflow_id,
                WorkflowRunStep.status == "completed",
            )
            .order_by(WorkflowRunStep.completed_at.desc().nulls_last())
            .limit(1)
        )
        gate_step = gate_result.first()
        if gate_step is None:
            continue
        decision = (gate_step.output or {}).get("decision")
        if decision == blocking_decision:
            blocking_signal = f"{gate_workflow_id}={decision}"
            break

    if blocking_signal is None:
        logger.debug(
            "arbitration trigger: no blocking gate signal for task %s "
            "(neither wf-review=changes_requested nor wf-validate=fail "
            "on latest run); no deadlock",
            row.task_id,
        )
        return None
    logger.info(
        "arbitration trigger: deadlock signal %r for task %s; dispatching "
        "wf-architecture-resolve",
        blocking_signal, row.task_id,
    )

    # Cap: 5 architecture-resolve dispatches per task across all sources.
    if await _is_capped(session, row.task_id, ARCHITECTURE_RESOLVE_WORKFLOW_ID):
        logger.warning(
            "arbitration trigger: %s capped for task %s "
            "(>=%d prior runs); operator must intervene",
            ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            row.task_id,
            ARCHITECTURE_RESOLVE_MAX_ATTEMPTS,
        )
        return None

    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    payload = {"repo": row.repo, "deadlock_feedback_run_id": str(row.run_id)}

    async def _dispatch() -> uuid.UUID | None:
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id=ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            trigger="self:wf-feedback-deadlock",
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id=ARCHITECTURE_RESOLVE_WORKFLOW_ID,
        payload=payload,
        dispatch_fn=_dispatch,
    )


async def maybe_dispatch_feedback_on_architect_amend(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """Fire ``wf-feedback`` when the architect verdicts ``amend``.

    ADR-0032 / ADR-0038 partnership closure: the architect's ``amend``
    verdict says "the intent is right; the code is the bug. A
    remediation plan will be drafted to fix the implementation." The
    architect emits a ``dispatch.workflow_id="wf-plan"`` hint in its
    payload but historically no consumer trigger acted on that hint —
    the verdict was decorative. This helper closes that gap with the
    lighter path: re-engage ``wf-feedback`` against the same task so
    the feedback role's analyzer + code-author can author the
    remediation directly without the heavier wf-plan ceremony.

    Skips cleanly when:
      * The completed step isn't ``wf-architecture-resolve`` or its
        decision isn't ``amend``.
      * The task has already dispatched ``wf-feedback`` >= 5 times
        (cap per ADR-0029 Q29.e).

    Dedup namespace: ``wf-feedback:<repo>:architect-amend-run=<wf_architecture_resolve_run_id>``.
    Each amend verdict from a distinct architect run produces at most
    one feedback dispatch.

    Returns the new ``wf-feedback`` run's id, or ``None`` if any skip
    condition fired.
    """
    if typed.output.decision != "amend":
        return None

    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.id.label("run_id"),
            WorkflowRun.task_id,
            Task.repo,
        )
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .join(
            WorkflowVersion,
            WorkflowVersion.id == WorkflowRun.workflow_version_id,
        )
        .join(Task, Task.id == WorkflowRun.task_id)
        .where(WorkflowRunStep.id == step_id)
    )
    row = result.first()
    if row is None or row.workflow_id != ARCHITECTURE_RESOLVE_WORKFLOW_ID:
        return None

    if await _is_capped(session, row.task_id, FEEDBACK_WORKFLOW_ID):
        logger.warning(
            "amend-feedback trigger: %s capped for task %s "
            "(>=%d prior runs); operator must intervene",
            FEEDBACK_WORKFLOW_ID, row.task_id, FEEDBACK_MAX_ATTEMPTS,
        )
        return None

    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    payload = {
        "repo": row.repo,
        "architect_amend_run_id": str(row.run_id),
    }

    async def _dispatch() -> uuid.UUID | None:
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id=FEEDBACK_WORKFLOW_ID,
            trigger="self:architect-amend",
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id=FEEDBACK_WORKFLOW_ID,
        payload=payload,
        dispatch_fn=_dispatch,
    )


_REVIEW_OVERRIDE_NAMESPACE = uuid.UUID("8e16f4c0-2c4c-4f4f-9b9a-7c3d6f1a5e10")


async def maybe_emit_review_override_on_architect_completion(
    session: AsyncSession,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """ADR-0038: emit a ``review.override`` Event row when an architect
    step.completed carries ``payload.dispatch.review_override == True``.

    The mergeability VIEW (post-0016 migration) reads ``review.override``
    events at HEAD as ``review_decision='approved'``, unblocking
    auto-merge for ralph-loop deadlocks the architect resolved with
    ``verdict='accept-as-is'``.

    The architect doesn't know the PR's HEAD sha. We look it up via the
    most-recent ``pr_opened``/``pr_synchronize`` event for the task's
    PR — the same join the VIEW uses. If the PR has since advanced past
    the HEAD the architect adjudicated, the override naturally targets
    the new HEAD; that's acceptable because the architect's
    ``accept-as-is`` verdict applies to the state of the diff it saw,
    and a later sync would have re-fired wf-review.

    Idempotency: the Event row's ``id`` is a deterministic UUIDv5 of
    ``(task_id, commit_sha)`` so re-delivery of the same step is a
    no-op via ``ON CONFLICT (id) DO NOTHING``.

    Returns the emitted Event id, or ``None`` if no emission happened.
    """
    output = typed.output
    payload = output.payload if isinstance(output.payload, dict) else {}
    dispatch = payload.get("dispatch") if isinstance(payload, dict) else None
    if not isinstance(dispatch, dict) or not dispatch.get("review_override"):
        return None

    # Confirm this step belongs to wf-architecture-resolve. Any other
    # workflow setting ``review_override`` is malformed; skip defensively.
    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.task_id,
        )
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .join(
            WorkflowVersion,
            WorkflowVersion.id == WorkflowRun.workflow_version_id,
        )
        .where(WorkflowRunStep.id == step_id)
    )
    row = result.first()
    if row is None or row.workflow_id != ARCHITECTURE_RESOLVE_WORKFLOW_ID:
        logger.warning(
            "review_override emission skipped: step %s is not "
            "wf-architecture-resolve (workflow=%s)",
            step_id, row.workflow_id if row else None,
        )
        return None

    # Resolve task PR + latest HEAD sha. Mirrors the
    # ``task_mergeability`` VIEW's head LATERAL: latest pr_opened or
    # pr_synchronize event for the task's (repo, pr_number) wins.
    pr_result = await session.execute(
        select(TaskPR.repo, TaskPR.pr_number).where(
            TaskPR.task_id == row.task_id,
        )
    )
    pr_row = pr_result.first()
    if pr_row is None:
        logger.warning(
            "review_override emission skipped: no task_pr for task %s",
            row.task_id,
        )
        return None

    head_result = await session.execute(
        select(Event.payload).where(
            Event.entity_type == "github",
            Event.action.in_(("pr_opened", "pr_synchronize")),
            Event.payload["repo"].astext == pr_row.repo,
            Event.payload["pr_number"].astext == str(pr_row.pr_number),
        )
        .order_by(Event.created_at.desc())
        .limit(1)
    )
    head_row = head_result.first()
    if head_row is None or not (head_row.payload or {}).get("head_sha"):
        logger.warning(
            "review_override emission skipped: no pr_opened/pr_synchronize "
            "event for task %s (repo=%s pr=%s)",
            row.task_id, pr_row.repo, pr_row.pr_number,
        )
        return None
    head_sha = head_row.payload["head_sha"]

    reasoning = payload.get("reasoning") or ""

    event_id = uuid.uuid5(
        _REVIEW_OVERRIDE_NAMESPACE,
        f"{row.task_id}:{head_sha}",
    )
    stmt = (
        pg_insert(Event)
        .values(
            id=event_id,
            entity_type="review",
            action="override",
            task_id=row.task_id,
            commit_sha=head_sha,
            payload={"commit_sha": head_sha, "reasoning": reasoning},
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.execute(stmt)
    logger.info(
        "review.override emitted: task=%s commit_sha=%s event_id=%s",
        row.task_id, head_sha, event_id,
    )
    return event_id


def is_docs_current_check_failure(output: Any) -> bool:
    """Return ``True`` if the step output payload contains a failing
    ``docs-current-with-pr`` check.

    Reads ``output.payload["checks"]`` (the list emitted by the
    validation worker per ADR-0029). A check is considered failing when
    its ``verdict`` is anything other than ``"pass"``. Only the
    ``docs-current-with-pr`` check_id is tested; other checks are ignored.

    Used by the coordination consumer to route wf-validate ``fail``
    verdicts: when this check is in the failing set the consumer dispatches
    ``wf-doc-amend``; otherwise it falls through to ``wf-feedback``.
    """
    checks = output.payload.get("checks", [])
    return any(
        check.get("check_id") == DOCS_CURRENT_CHECK_ID
        and check.get("verdict") not in ("pass", None)
        for check in checks
    )


async def maybe_dispatch_doc_amend_on_docs_check_fail(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """Fire ``wf-doc-amend`` when ``wf-validate.step.completed`` arrives
    with ``decision='fail'`` and the ``docs-current-with-pr`` check is
    among the failing checks.

    Fourth dispatch source — mirrors ADR-0029's third-source pattern but
    routes to ``wf-doc-amend`` rather than ``wf-feedback``:

      * ``wf-validate.step.completed`` with ``decision='fail'`` AND
        ``docs-current-with-pr`` check present in failing set
        → dispatch ``wf-doc-amend``.
      * Different check failures (no ``docs-current-with-pr`` in the
        failing set) continue to dispatch ``wf-feedback`` via the
        existing ``maybe_dispatch_feedback_on_terminal_failure`` path.

    Skips cleanly when:
      * The step's ``decision`` is not ``'fail'``.
      * The owning run's ``workflow_id`` is not ``wf-validate``.
      * Owning task can't be resolved (deleted between dispatch + completion).
      * No ``WorkflowVersion`` exists for ``wf-doc-amend`` (un-seeded install).
      * Task has already dispatched ``wf-doc-amend`` >= 5 times (cap).

    Dedup is gated on:
      ``wf-doc-amend:<repo>:docs-amend-run=<wf_validate_run_id>``

    so each wf-validate run triggers at most one doc-amend attempt.

    Returns the new ``wf-doc-amend`` run's id, or ``None`` if any skip
    condition fired.
    """
    if typed.output.decision != "fail":
        return None

    # Resolve (workflow_id, run_id, task_id, repo) for this step.
    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.id.label("run_id"),
            WorkflowRun.task_id,
            Task.repo,
        )
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .join(
            WorkflowVersion,
            WorkflowVersion.id == WorkflowRun.workflow_version_id,
        )
        .join(Task, Task.id == WorkflowRun.task_id)
        .where(WorkflowRunStep.id == step_id)
    )
    row = result.first()
    if row is None:
        logger.debug(
            "doc-amend trigger: no run/task resolvable for step %s; skipping",
            step_id,
        )
        return None
    if row.workflow_id != "wf-validate":
        return None

    # Check cap: skip if this task has already dispatched wf-doc-amend
    # >= DOC_AMEND_MAX_ATTEMPTS times.
    if await _is_capped(session, row.task_id, DOC_AMEND_WORKFLOW_ID):
        logger.warning(
            "doc-amend trigger: %s capped for task %s (>=%d prior runs); skipping",
            DOC_AMEND_WORKFLOW_ID, row.task_id, DOC_AMEND_MAX_ATTEMPTS,
        )
        return None

    # Re-fetch the Task so the dispatch helper has the full ORM object.
    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    # Dedup namespace: docs-amend-run=<wf_validate_run_id> — one
    # doc-amend attempt per wf-validate run that failed the check.
    payload = {"repo": row.repo, "docs_amend_run_id": str(row.run_id)}

    async def _dispatch() -> uuid.UUID | None:
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id=DOC_AMEND_WORKFLOW_ID,
            trigger="self:wf-validate-docs-current-fail",
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id=DOC_AMEND_WORKFLOW_ID,
        payload=payload,
        dispatch_fn=_dispatch,
    )


async def _create_and_publish_run(
    session: AsyncSession,
    dispatcher: Any,
    *,
    task: Task,
    workflow_id: str,
    trigger: str,
) -> uuid.UUID | None:
    """Create a WorkflowRun + step rows for the workflow and publish
    ``step.ready`` for the first step.

    Mirrors bunkhouse ``TriggerEvaluator._create_workflow_run`` adapted
    to Treadmill's schema: ``WorkflowRun.workflow_version_id`` (not
    ``workflow_id``), and the ``trigger`` column is one string (not the
    bunkhouse pair of ``trigger`` + ``trigger_event``). Callers encode
    the source + event into the ``trigger`` value
    (e.g. ``webhook:pr_synchronize`` for the github path,
    ``self:wf-review-changes-requested`` for the step-output path) so
    the audit trail still tells you what fired the run.

    Returns the run's id, or ``None`` if the workflow has no version
    (un-seeded install) or no steps (degenerate workflow).
    """
    # Resolve the latest WorkflowVersion for ``workflow_id``. Per
    # ADR-0010, versions are immutable; ``version_strategy='latest'``
    # picks the highest version number.
    wv_result = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    wv = wv_result.scalar_one_or_none()
    if wv is None:
        logger.warning(
            "trigger evaluator: no WorkflowVersion for %s; skipping dispatch "
            "for task %s (run starters seed?)",
            workflow_id, task.id,
        )
        return None

    # Snapshot the version's steps. The order matters; the first step
    # gets the ``step.ready`` event and the SQS claim.
    steps_result = await session.execute(
        select(WorkflowVersionStep)
        .where(WorkflowVersionStep.workflow_version_id == wv.id)
        .order_by(WorkflowVersionStep.step_index)
    )
    wv_steps = list(steps_result.scalars())
    if not wv_steps:
        logger.warning(
            "trigger evaluator: WorkflowVersion %s has no steps; skipping "
            "dispatch for task %s",
            wv.id, task.id,
        )
        return None

    # Create the WorkflowRun.
    run = WorkflowRun(
        task_id=task.id,
        workflow_version_id=wv.id,
        trigger=trigger,
    )
    session.add(run)
    await session.flush()

    # Materialize every step row up front (mirroring ``dispatch_task``
    # so cross-step dispatch (B.2) finds them as ``pending``).
    run_steps: list[WorkflowRunStep] = []
    for wv_step in wv_steps:
        rs = WorkflowRunStep(
            run_id=run.id,
            step_index=wv_step.step_index,
            step_name=wv_step.step_name,
            role_id=wv_step.role_id,
            status="pending",
        )
        session.add(rs)
        run_steps.append(rs)
    await session.flush()

    # Publish ``step.ready`` for the first step.
    first_step = run_steps[0]
    payload = StepReady(
        role_id=first_step.role_id,
        step_index=first_step.step_index,
        step_name=first_step.step_name,
        repo=task.repo,
        workflow_id=workflow_id,
    )

    # Persist the Event row first (durable source of truth). The
    # dispatcher's ``_persist_event`` is the shared seam — it also
    # populates ``commit_sha`` (left ``None`` here; the github event's
    # commit_sha lives on the github Event row, not the step.ready).
    ready_event = await dispatcher._persist_event(
        session,
        entity_type="step",
        action="ready",
        payload=payload,
        plan_id=task.plan_id,
        task_id=task.id,
        run_id=run.id,
        step_id=first_step.id,
    )
    try:
        await dispatcher.publisher.publish(ready_event, payload)
    except Exception as exc:
        logger.exception(
            "trigger evaluator: failed to publish step.ready for run %s; "
            "Event row %s persisted, replay loop will retry",
            run.id, ready_event.id,
        )
        await dispatcher._record_publish_failed(
            session,
            original_event_id=ready_event.id,
            target="sns",
            error=exc,
            plan_id=task.plan_id,
            task_id=task.id,
            run_id=run.id,
            step_id=first_step.id,
        )

    # Send the SQS work-queue claim (same shape as ``dispatch_task``).
    if dispatcher.sqs_client is not None and dispatcher.work_queue_url is not None:
        try:
            await asyncio.to_thread(
                dispatcher.sqs_client.send_message,
                QueueUrl=dispatcher.work_queue_url,
                MessageBody=json.dumps({
                    "step_id": str(first_step.id),
                    "task_id": str(task.id),
                    "plan_id": str(task.plan_id),
                    "run_id": str(run.id),
                }),
                MessageGroupId=str(run.id),
                MessageAttributes=inject_trace_context(),
            )
        except Exception as exc:
            logger.exception(
                "trigger evaluator: failed to send work-queue claim for run %s; "
                "replay loop will retry",
                run.id,
            )
            await dispatcher._record_publish_failed(
                session,
                original_event_id=ready_event.id,
                target="sqs",
                error=exc,
                plan_id=task.plan_id,
                task_id=task.id,
                run_id=run.id,
                step_id=first_step.id,
            )

    logger.info(
        "trigger evaluator: dispatched %s for task %s (run %s) on %s",
        workflow_id, task.id, run.id, trigger,
    )
    return run.id


# ── Scheduled-tick dispatch (ADR-0035) ───────────────────────────────────────


async def handle_scheduled_tick(
    session: AsyncSession,
    dispatcher: Any,
    *,
    typed: ScheduledTick,
) -> uuid.UUID | None:
    """Handle a ``scheduled.tick`` event: look up the schedule, validate it
    is still active, and dispatch the bound workflow's first step.

    The scheduler emits one of these per cron fire; the consumer's
    ``schedule`` branch routes it here after the Pydantic parse gate.

    Schedules don't inherently have a task context (ops bots sweep the
    entire repo, not a specific PR). ``_create_and_publish_run_without_task``
    creates the run with ``task_id=None`` — this path works in unit tests
    (mocked sessions skip the NOT NULL DB check) and requires a schema
    migration making ``workflow_runs.task_id`` nullable before use against a
    real database.

    Returns the new run's id, or ``None`` if any skip condition fired
    (schedule not found, paused, no WorkflowVersion seeded).
    """
    schedule = await session.get(Schedule, typed.schedule_id)
    if schedule is None:
        logger.warning(
            "scheduled-tick: schedule %s not found; dropping tick",
            typed.schedule_id,
        )
        return None
    if schedule.status != "active":
        logger.info(
            "scheduled-tick: schedule %s is %s; skipping dispatch",
            typed.schedule_id, schedule.status,
        )
        return None

    repo = typed.rendered_payload.get("repo", "")
    return await _create_and_publish_run_without_task(
        session,
        dispatcher,
        workflow_id=schedule.workflow_id,
        trigger=f"schedule:{typed.schedule_id}",
        repo=repo,
    )


async def _create_and_publish_run_without_task(
    session: AsyncSession,
    dispatcher: Any,
    *,
    workflow_id: str,
    trigger: str,
    repo: str,
) -> uuid.UUID | None:
    """Create a WorkflowRun + step rows for a schedule-triggered dispatch
    and publish ``step.ready`` for the first step.

    Mirrors ``_create_and_publish_run`` but accepts no Task context.
    ``WorkflowRun.task_id`` is set to ``None``; this works in unit tests
    (mocked sessions) and requires the column to be nullable in production.

    Returns the run's id, or ``None`` if the workflow has no version
    (un-seeded install) or no steps (degenerate workflow).
    """
    wv_result = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    wv = wv_result.scalar_one_or_none()
    if wv is None:
        logger.warning(
            "scheduled-tick: no WorkflowVersion for %s; skipping dispatch "
            "(run starters seed?)",
            workflow_id,
        )
        return None

    steps_result = await session.execute(
        select(WorkflowVersionStep)
        .where(WorkflowVersionStep.workflow_version_id == wv.id)
        .order_by(WorkflowVersionStep.step_index)
    )
    wv_steps = list(steps_result.scalars())
    if not wv_steps:
        logger.warning(
            "scheduled-tick: WorkflowVersion %s has no steps; skipping "
            "dispatch for workflow %s",
            wv.id, workflow_id,
        )
        return None

    run = WorkflowRun(
        task_id=None,
        workflow_version_id=wv.id,
        trigger=trigger,
    )
    session.add(run)
    await session.flush()

    run_steps: list[WorkflowRunStep] = []
    for wv_step in wv_steps:
        rs = WorkflowRunStep(
            run_id=run.id,
            step_index=wv_step.step_index,
            step_name=wv_step.step_name,
            role_id=wv_step.role_id,
            status="pending",
        )
        session.add(rs)
        run_steps.append(rs)
    await session.flush()

    first_step = run_steps[0]
    step_payload = StepReady(
        role_id=first_step.role_id,
        step_index=first_step.step_index,
        step_name=first_step.step_name,
        repo=repo,
        workflow_id=workflow_id,
    )

    ready_event = await dispatcher._persist_event(
        session,
        entity_type="step",
        action="ready",
        payload=step_payload,
        plan_id=None,
        task_id=None,
        run_id=run.id,
        step_id=first_step.id,
    )
    try:
        await dispatcher.publisher.publish(ready_event, step_payload)
    except Exception as exc:
        logger.exception(
            "scheduled-tick: failed to publish step.ready for run %s; "
            "Event row %s persisted, replay loop will retry",
            run.id, ready_event.id,
        )
        await dispatcher._record_publish_failed(
            session,
            original_event_id=ready_event.id,
            target="sns",
            error=exc,
            plan_id=None,
            task_id=None,
            run_id=run.id,
            step_id=first_step.id,
        )

    if dispatcher.sqs_client is not None and dispatcher.work_queue_url is not None:
        try:
            await asyncio.to_thread(
                dispatcher.sqs_client.send_message,
                QueueUrl=dispatcher.work_queue_url,
                MessageBody=json.dumps({
                    "step_id": str(first_step.id),
                    "task_id": None,
                    "plan_id": None,
                    "run_id": str(run.id),
                }),
                MessageGroupId=str(run.id),
                MessageAttributes=inject_trace_context(),
            )
        except Exception as exc:
            logger.exception(
                "scheduled-tick: failed to send work-queue claim for run %s; "
                "replay loop will retry",
                run.id,
            )
            await dispatcher._record_publish_failed(
                session,
                original_event_id=ready_event.id,
                target="sqs",
                error=exc,
                plan_id=None,
                task_id=None,
                run_id=run.id,
                step_id=first_step.id,
            )

    logger.info(
        "scheduled-tick: dispatched %s (run %s) on %s",
        workflow_id, run.id, trigger,
    )
    return run.id


# ── Auto-merge cooling-off trigger (ADR-0031) ─────────────────────────────────

AUTO_MERGE_DEADLINE_KEY_PREFIX = "treadmill:auto-merge-deadline:"
AUTO_MERGE_FIRED_KEY_PREFIX = "treadmill:auto-merge-fired:"
AUTO_MERGE_COOLDOWN_SECONDS = 30

_AUTO_MERGE_KEY_TTL_SECONDS = AUTO_MERGE_COOLDOWN_SECONDS + 60
_AUTO_MERGE_FIRED_TTL_SECONDS = 86400  # 24h

# Only these workflow completions drive the cooling-off window.
_AUTO_MERGE_TRIGGER_WORKFLOWS: frozenset[str] = frozenset(
    {"wf-validate", "wf-review"}
)


async def maybe_auto_merge_on_mergeable(
    session: AsyncSession,
    redis_client: Any,
    *,
    step_id: str,
) -> bool:
    """Set/push the 30-second cooling-off deadline when wf-validate or
    wf-review completes and the task's mergeability VIEW reads ``mergeable``.

    Per ADR-0031, the auto-merge trigger fires on the
    ``mergeability.changed.mergeable`` projection — implemented here as a
    check of the VIEW after each wf-validate / wf-review step completion.
    The 30-second window absorbs event races between the two workflows.

    Skip conditions (any one short-circuits):
      * Not a wf-validate or wf-review step.
      * ``redis_client`` not wired (auto-merge poll loop inoperable).
      * ``plan.auto_merge IS FALSE`` — plan has opted out (ADR-0031 Q31.c).
      * ``derived_mergeability != 'mergeable'`` — task is not ready.
      * ``validate_decision != 'pass'`` — ADR-0031 Q31.b: ``uncertain``
        does NOT auto-merge; routes to wf-feedback for rework instead.
      * ``review_decision != 'approved'`` — pending human review.
      * ``treadmill:auto-merge-fired:<task_id>`` key exists in Redis —
        merge already dispatched for this task.

    On pass: writes (or overwrites) ``treadmill:auto-merge-deadline:<task_id>``
    as a JSON blob with ``deadline_at = now + 30s``. Subsequent calls while
    the task remains mergeable push the deadline forward, absorbing the
    wf-validate / wf-review completion race. The consumer's 5-second poll
    loop (``fire_elapsed_auto_merges``) consumes this key.

    Returns ``True`` if the deadline was set/pushed, ``False`` otherwise.
    """
    if redis_client is None:
        return False

    # Resolve workflow_id + task_id from the completing step.
    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.task_id,
        )
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
        .where(WorkflowRunStep.id == step_id)
    )
    row = result.first()
    if row is None:
        logger.debug(
            "auto-merge: no run/task for step %s; skipping", step_id,
        )
        return False

    if row.workflow_id not in _AUTO_MERGE_TRIGGER_WORKFLOWS:
        return False

    task_id = row.task_id

    # Skip if auto-merge was already dispatched for this task.
    fired_key = AUTO_MERGE_FIRED_KEY_PREFIX + str(task_id)
    if await redis_client.exists(fired_key):
        logger.debug(
            "auto-merge: already fired for task %s; skipping", task_id,
        )
        return False

    # Query mergeability VIEW + plan.auto_merge in one pass.
    merge_result = await session.execute(
        text("""
            SELECT
                tm.derived_mergeability,
                tm.validate_decision,
                tm.review_decision,
                tm.repo,
                tm.pr_number,
                p.auto_merge
            FROM task_mergeability tm
            JOIN tasks t ON t.id = tm.task_id
            JOIN plans p ON p.id = t.plan_id
            WHERE tm.task_id = CAST(:task_id AS uuid)
        """),
        {"task_id": str(task_id)},
    )
    merge_row = merge_result.first()
    if merge_row is None:
        logger.debug(
            "auto-merge: no mergeability row for task %s; skipping", task_id,
        )
        return False

    # plan.auto_merge=false → plan has opted out.
    if merge_row.auto_merge is False:
        logger.debug(
            "auto-merge: plan.auto_merge=false for task %s; skipping", task_id,
        )
        return False

    # Not currently mergeable.
    if merge_row.derived_mergeability != "mergeable":
        logger.debug(
            "auto-merge: task %s mergeability=%r; skipping",
            task_id, merge_row.derived_mergeability,
        )
        return False

    # ADR-0031 Q31.b: only 'pass' auto-merges; 'uncertain' routes to wf-feedback.
    if merge_row.validate_decision != "pass":
        logger.debug(
            "auto-merge: validate_decision=%r for task %s; skipping",
            merge_row.validate_decision, task_id,
        )
        return False

    # Pending human review (review_decision must be 'approved').
    if merge_row.review_decision != "approved":
        logger.debug(
            "auto-merge: review_decision=%r for task %s; skipping",
            merge_row.review_decision, task_id,
        )
        return False

    # All checks pass — set/push the 30-second cooling-off deadline.
    deadline_at = datetime.now(timezone.utc) + timedelta(
        seconds=AUTO_MERGE_COOLDOWN_SECONDS
    )
    deadline_key = AUTO_MERGE_DEADLINE_KEY_PREFIX + str(task_id)
    value = json.dumps({
        "task_id": str(task_id),
        "repo": merge_row.repo,
        "pr_number": merge_row.pr_number,
        "deadline_at": deadline_at.isoformat(),
    })
    await redis_client.set(
        deadline_key,
        value,
        ex=_AUTO_MERGE_KEY_TTL_SECONDS,
    )
    logger.info(
        "auto-merge: deadline set for task %s (repo=%s pr=%d deadline=%s)",
        task_id, merge_row.repo, merge_row.pr_number, deadline_at.isoformat(),
    )
    return True


async def fire_elapsed_auto_merges(
    redis_client: Any,
    sessionmaker: Any,
    github_client: Any,
) -> int:
    """Scan Redis for elapsed auto-merge deadlines and fire the GitHub merge.

    Called every 5 seconds by the consumer's auto-merge poll loop. For each
    ``treadmill:auto-merge-deadline:<task_id>`` key whose ``deadline_at`` is
    in the past:

      1. Re-verifies mergeability via the VIEW (a new push may have
         invalidated the prior thumbs since the deadline was set).
      2. Issues ``PUT /repos/{repo}/pulls/{pr_number}/merge`` with
         ``merge_method=squash`` via the GitHub API client.
      3. Marks the task as fired (``treadmill:auto-merge-fired:<task_id>``
         with a 24h TTL) and deletes the deadline key.

    Merge failures are logged and retried on the next 5s tick (the deadline
    key remains until a successful fire or the TTL expires).

    Returns the count of merges fired this tick.
    """
    if redis_client is None or github_client is None:
        return 0

    fired = 0
    cursor = 0
    now = datetime.now(timezone.utc)

    while True:
        cursor, keys = await redis_client.scan(
            cursor, match=AUTO_MERGE_DEADLINE_KEY_PREFIX + "*", count=100,
        )
        for raw_key in keys:
            did_fire = await _process_deadline_key(
                redis_client=redis_client,
                sessionmaker=sessionmaker,
                github_client=github_client,
                raw_key=raw_key,
                now=now,
            )
            if did_fire:
                fired += 1

        if cursor == 0:
            break

    return fired


async def _process_deadline_key(
    redis_client: Any,
    sessionmaker: Any,
    github_client: Any,
    raw_key: Any,
    now: datetime,
) -> bool:
    """Process one deadline key from the auto-merge Redis scan.

    Returns ``True`` if a merge was fired, ``False`` otherwise.
    All exceptions are swallowed so the caller's scan loop continues.
    """
    key_str = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
    raw_val = await redis_client.get(raw_key)
    if raw_val is None:
        return False

    try:
        data = json.loads(raw_val)
    except (ValueError, TypeError):
        logger.warning("auto-merge poll: malformed key %s; deleting", key_str)
        await redis_client.delete(raw_key)
        return False

    task_id_str = data.get("task_id")
    repo = data.get("repo")
    pr_number = data.get("pr_number")
    deadline_str = data.get("deadline_at")

    if not task_id_str or not repo or pr_number is None or not deadline_str:
        await redis_client.delete(raw_key)
        return False

    try:
        task_id = uuid.UUID(task_id_str)
        deadline_at = datetime.fromisoformat(deadline_str)
    except (ValueError, AttributeError):
        await redis_client.delete(raw_key)
        return False

    if deadline_at > now:
        return False  # Still in the cooling-off window.

    # Re-verify mergeability before firing — a new push may have invalidated.
    try:
        async with sessionmaker() as session:
            still_ok = await _check_still_mergeable_for_auto_merge(
                session, task_id,
            )
    except Exception:
        logger.exception(
            "auto-merge poll: mergeability re-check failed for task %s; "
            "skipping this tick",
            task_id,
        )
        return False

    if not still_ok:
        logger.info(
            "auto-merge poll: task %s no longer mergeable; clearing deadline",
            task_id,
        )
        await redis_client.delete(raw_key)
        return False

    # Fire the merge via the GitHub REST API.
    try:
        response = await github_client.put(
            f"/repos/{repo}/pulls/{pr_number}/merge",
            json={"merge_method": "squash"},
        )
        response.raise_for_status()
    except Exception:
        logger.exception(
            "auto-merge poll: GitHub merge failed for task %s "
            "(repo=%s pr=%s); will retry on next tick",
            task_id, repo, pr_number,
        )
        return False

    # Mark as fired + remove the deadline key.
    fired_key = AUTO_MERGE_FIRED_KEY_PREFIX + str(task_id)
    await redis_client.set(fired_key, b"1", ex=_AUTO_MERGE_FIRED_TTL_SECONDS)
    await redis_client.delete(raw_key)
    logger.info(
        "auto-merge poll: merged task %s (repo=%s pr=%d)",
        task_id, repo, pr_number,
    )
    return True


async def _check_still_mergeable_for_auto_merge(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> bool:
    """Re-verify mergeability + plan opt-out before the poll loop fires a merge.

    Returns ``True`` only when the VIEW still reads ``mergeable`` and the
    plan hasn't opted out.
    """
    result = await session.execute(
        text("""
            SELECT tm.derived_mergeability, p.auto_merge
            FROM task_mergeability tm
            JOIN tasks t ON t.id = tm.task_id
            JOIN plans p ON p.id = t.plan_id
            WHERE tm.task_id = CAST(:task_id AS uuid)
        """),
        {"task_id": str(task_id)},
    )
    row = result.first()
    if row is None:
        return False
    if row.auto_merge is False:
        return False
    return row.derived_mergeability == "mergeable"
