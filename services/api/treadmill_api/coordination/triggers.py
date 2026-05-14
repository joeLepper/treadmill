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
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.coordination.dispatch_dedup import maybe_dispatch_with_dedup
from treadmill_api.events.step import StepCompleted, StepReady
from treadmill_api.models import (
    EventTrigger,
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
CI_FIX_MAX_ATTEMPTS = 3
CONFLICT_RESOLVE_MAX_ATTEMPTS = 3
FEEDBACK_MAX_ATTEMPTS = 5


# Conclusions that represent a genuine CI failure — the only ones that
# should fire the CI-fix workflow. Mirrors bunkhouse's
# ``FAILURE_CONCLUSIONS`` frozenset; ``success``, ``neutral``,
# ``cancelled``, ``skipped``, and ``stale`` are intentionally excluded.
FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure"}
)


# Per-event extra fan-out: ``pr_synchronize`` fires ``wf-review`` (via the
# event_triggers row) AND ``wf-validate``. The table only supports one
# workflow per (repo, event_type), so the second workflow is named here.
# Empty for every other event_type.
_EXTRA_FANOUT_WORKFLOWS: dict[str, list[str]] = {
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

    Handles three scenarios:
      1. ``wf-review`` with ``decision='changes_requested'`` (task #108 path 1)
      2. ``wf-validate`` with ``decision='fail'`` (ADR-0029)
      3. ``wf-validate`` with ``decision='error'`` (ADR-0029)

    Companion to the github-webhook-driven ``evaluate_triggers``: that
    function fires ``wf-feedback`` on a ``pr_review_submitted`` event
    with ``state='changes_requested'`` (human reviewer outside Treadmill).
    This function fires it on Treadmill's own verdicts (self-review via
    ``wf-review`` which no longer emits ``pr_review_submitted`` webhook,
    and validation failures via ``wf-validate``).

    Skips cleanly when:
      * The completed step's workflow doesn't match ``workflow_id``.
      * The envelope's ``decision`` isn't ``fail_decision``.
      * Owning task can't be resolved (deleted between dispatch + completion).
      * No ``WorkflowVersion`` exists for ``wf-feedback`` (un-seeded install).
      * Task has already dispatched wf-feedback >= 5 times (cap per ADR-0029 Q29.e).

    Dedup is gated by ADR-0026's ``WorkflowDispatchDedup`` table keyed on:
      * ``wf-feedback:<repo>:review-run=<wf_review_run_id>`` for wf-review
      * ``wf-feedback:<repo>:validate-run=<wf_validate_run_id>`` for wf-validate

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
