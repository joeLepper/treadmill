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
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.dialects.postgresql import insert as pg_insert

from treadmill_api.coordination.coordinator_overlay import (
    CapOverlayDecision,
    coordinator_overlay_decision,
)
from treadmill_api.coordination.dispatch_dedup import maybe_dispatch_with_dedup
from treadmill_api.observability import inject_trace_context
from treadmill_api.events.step import StepCompleted, StepReady
from treadmill_api.events.schedule import ScheduledTick
from treadmill_api.events.task import (
    ArchitectEmitFailure,
    TaskCancelled,
    TaskEscalatedToOperator,
    TaskRegistered,
)
from treadmill_api.onboarding_store import OnboardingStore
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
from treadmill_api.seed.system_plan import SYSTEM_PLAN_ID

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
AUTHOR_WORKFLOW_ID = "wf-author"
CI_FIX_MAX_ATTEMPTS = 3
CONFLICT_RESOLVE_MAX_ATTEMPTS = 3
FEEDBACK_MAX_ATTEMPTS = 5
DOC_AMEND_MAX_ATTEMPTS = 5
# ADR-0029 introduced the architect-amend cap at 5. Reduced to 3 on
# 2026-06-06 after ADR-0074 (deterministic nothing-to-do short-circuit)
# and ADR-0081 (worker→operator hint channel) shipped — the high cap
# was insulating against failure classes those now address cheaper.
# Per the 2026-06-05 audit: 17/32 tasks/week hit the 4-5 bucket
# (8 merged, 7 cancelled, 2 open). With ADR-0074 catching empty-amend
# loops AND ADR-0081 letting the operator inject hints mid-loop, the
# remaining true "needs > 3 cycles" cases drop to a handful — and
# those rescue via operator-merge or hand-author per the 2026-06-05
# night-session pattern. Lower cap shrinks session-limit blast radius
# (alan+donna hit personal-Anthropic limits on the high-cycle tasks).
ARCHITECTURE_RESOLVE_MAX_ATTEMPTS = 3

# The check_id that routes wf-validate failures to wf-doc-amend instead
# of wf-feedback. Any other check failure still dispatches wf-feedback.
DOCS_CURRENT_CHECK_ID = "docs-current-with-pr"

# The exact error message raised by code.py when Claude Code produces no
# diff — used to route step.failed events to wf-architecture-resolve
# instead of wf-feedback (which requires an existing PR to remediate).
_NO_CHANGES_ERROR_SIGNATURE = "Claude Code produced no changes to commit"

# Substring present in git push stderr when GitHub (or the lease check)
# rejects the push — branch protection rule, stale force-with-lease, etc.
# Appears in both "[remote rejected]" and "[rejected] ... (stale info)"
# cases because git always appends "error: failed to push some refs to ..."
# when any ref is rejected.  Used to route step.failed to
# wf-architecture-resolve (ADR-0048) so the architect can supersede the
# rejected branch rather than cycling through wf-feedback.
_REMOTE_REJECTION_ERROR_SIGNATURE = "failed to push some refs"


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
        # ADR-0084 Task 2B — coordinator overlay runs BEFORE the cap
        # check. When the plan has an active blocked_operator task
        # (coordinator has escalated, is alive), we skip dispatch
        # entirely; the coordinator will release when the operator
        # decides. When the coordinator is absent (no rows or stale
        # updated_at), the overlay short-circuits and the existing cap
        # body fires as before. The cap is the hard backstop; the
        # overlay never relaxes it.
        overlay = await coordinator_overlay_decision(session, task.id)
        if overlay is CapOverlayDecision.BLOCK_BY_COORDINATOR:
            logger.info(
                "trigger evaluator: %s blocked by coordinator for task %s "
                "(plan has blocked_operator status); skipping dispatch for %s",
                workflow_id, task.id, event_type,
            )
            continue

        # Cap policy gate. Capped workflows that hit the limit log a
        # warning + return None instead of dispatching.
        if await _is_capped(session, task.id, workflow_id):
            logger.warning(
                "trigger evaluator: %s capped for task %s (>=%d prior runs); "
                "skipping dispatch for %s",
                workflow_id, task.id, _cap_for(workflow_id), event_type,
            )
            # SDE-3/4: ci-fix / conflict caps leave the PR gate-blocked with
            # no further recovery — surface to operator rather than silently
            # dropping the candidate. (Other capped workflows re-dispatch via
            # their own paths or are inherently non-terminal.)
            if workflow_id in (CI_FIX_WORKFLOW_ID, CONFLICT_WORKFLOW_ID):
                await _emit_operator_escalation(
                    session,
                    dispatcher,
                    task_id=task.id,
                    repo=task.repo,
                    signal=f"{workflow_id}-cap-reached",
                    detail=(
                        f"{workflow_id} reached its {_cap_for(workflow_id)}-"
                        "attempt cap; operator intervention needed."
                    ),
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
    if workflow_id == ARCHITECTURE_RESOLVE_WORKFLOW_ID:
        return ARCHITECTURE_RESOLVE_MAX_ATTEMPTS
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


# Terminal derived_status values per the task_status VIEW (ADR-0011). When a
# task reaches one of these, there is no non-terminal workflow to retry.
_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"pr_merged", "cancelled", "done"})


async def infer_retry_workflow(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> str | None:
    """Return the workflow_id of the most-recent non-terminal workflow run
    for this task, or ``None`` when no eligible run exists.

    Returns ``None`` when:
    - The task has no workflow runs.
    - The task's derived_status (task_status VIEW) is in
      ``{'pr_merged', 'cancelled', 'done'}`` — the operator should pass
      ``--workflow`` explicitly when the task is already terminal.

    Used by the ``treadmill task retry`` CLI path (ADR-0046) to infer which
    workflow to re-dispatch when the operator omits ``--workflow``.
    """
    result = await session.execute(
        select(WorkflowVersion.workflow_id)
        .join(WorkflowRun, WorkflowRun.workflow_version_id == WorkflowVersion.id)
        .where(WorkflowRun.task_id == task_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None

    status_result = await session.execute(
        text("SELECT derived_status FROM task_status WHERE id = CAST(:task_id AS uuid)"),
        {"task_id": str(task_id)},
    )
    status_row = status_result.first()
    if status_row is None:
        return None
    if status_row.derived_status in _TERMINAL_TASK_STATUSES:
        return None
    return row.workflow_id


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


async def maybe_dispatch_feedback_on_step_failed(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    workflow_id: str,
) -> uuid.UUID | None:
    """Dispatch wf-feedback or wf-architecture-resolve when a wf-author
    step ends as ``step.failed`` (silent-death failure mode).

    Companion to ``maybe_dispatch_feedback_on_terminal_failure``: that
    one fires on ``step.completed`` with ``decision='fail'`` (the worker
    completed but reported the task failed). This one fires on
    ``step.failed`` (the worker crashed or the agent died mid-execution
    before producing a verdict). Captured 2026-05-18 on task
    ``07c71852``: wf-author worker terminated as ``step.failed`` with
    no summary; ADR-0037's existing trigger only checks the
    decision='fail' path so the system gave up and the task escalated
    to manual operator nudge.

    Routing is based on the step's ``error`` text (ADR-0048):

      * No-changes error (``_NO_CHANGES_ERROR_SIGNATURE``): routes to
        ``wf-architecture-resolve`` via
        ``maybe_dispatch_architect_on_author_no_diff``. The author had
        nothing to commit — no PR was ever opened, so wf-feedback has
        nothing to remediate. The architect should review the task spec.

      * Any other error (worker crash, author-validations rejected):
        routes to ``wf-feedback`` as before, with the
        ``author-fail-run=<run_id>`` dedup namespace.

    Currently scoped to ``workflow_id='wf-author'`` because that's the
    only workflow whose step.failed shape is operator-observed.
    Extending to other workflows is a one-line change in the caller.
    """
    if workflow_id != "wf-author":
        # Other workflows' step.failed paths are not yet auto-retry.
        # Adding them is a one-line caller change; not blanket-enabled
        # to avoid surprise retries on workflows that intentionally let
        # step.failed terminate (e.g. wf-architecture-resolve which
        # already has its own rework_attempt cap path).
        return None

    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.id.label("run_id"),
            WorkflowRun.task_id,
            Task.repo,
            WorkflowRunStep.error.label("step_error"),
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
            "feedback (step.failed): no run/task resolvable for step %s; skipping",
            step_id,
        )
        return None
    if row.workflow_id != workflow_id:
        return None

    # Route: no-diff errors go to the architect; other failures go to feedback.
    if row.step_error and _NO_CHANGES_ERROR_SIGNATURE in row.step_error:
        return await maybe_dispatch_architect_on_author_no_diff(
            session, dispatcher, step_id=step_id, workflow_id=workflow_id
        )

    # Route: remote-rejected push goes to the architect (ADR-0048).
    if row.step_error and _REMOTE_REJECTION_ERROR_SIGNATURE in row.step_error:
        return await maybe_dispatch_architect_on_author_remote_rejection(
            session, dispatcher, step_id=step_id, workflow_id=workflow_id
        )

    if await _is_capped(session, row.task_id, FEEDBACK_WORKFLOW_ID):
        logger.warning(
            "feedback (step.failed): %s capped for task %s (>=%d prior runs); skipping",
            FEEDBACK_WORKFLOW_ID, row.task_id, FEEDBACK_MAX_ATTEMPTS,
        )
        return None

    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    payload = {"repo": row.repo, "author_run_id": str(row.run_id)}

    async def _dispatch() -> uuid.UUID | None:
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id="wf-feedback",
            trigger="self:wf-author-step-failed",
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id="wf-feedback",
        payload=payload,
        dispatch_fn=_dispatch,
    )


async def maybe_dispatch_architect_on_author_no_diff(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    workflow_id: str,
) -> uuid.UUID | None:
    """Dispatch ``wf-architecture-resolve`` when a wf-author step.failed
    carries the no-changes error — the author produced no diff (ADR-0048).

    The no-diff case means the task spec may be wrong or already complete:
    no PR was ever opened, so wf-feedback (which requires an existing PR
    to remediate) has nothing to work with. The architect reviews the task
    spec and verdicts one of: amend (iterate with hint), supersede (rewrite
    task text + restart), or accept-as-is (work is genuinely done elsewhere).

    Skips cleanly when:
      * ``workflow_id`` is not ``'wf-author'``.
      * The step's error doesn't contain ``_NO_CHANGES_ERROR_SIGNATURE``.
      * Owning task can't be resolved (deleted between dispatch + completion).
      * No ``WorkflowVersion`` exists for ``wf-architecture-resolve``
        (un-seeded install).
      * Task has already dispatched ``wf-architecture-resolve`` >=
        ``ARCHITECTURE_RESOLVE_MAX_ATTEMPTS`` times (cap).

    Dedup key:
        ``wf-architecture-resolve:<repo>:author-no-diff-run=<wf_author_run_id>``

    Distinct from the deadlock-feedback-run namespace so both trigger
    sources can fire for the same task when each has its own run.

    Returns the new ``wf-architecture-resolve`` run's id, or ``None``
    if any skip condition fired.
    """
    if workflow_id != AUTHOR_WORKFLOW_ID:
        return None

    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.id.label("run_id"),
            WorkflowRun.task_id,
            Task.repo,
            WorkflowRunStep.error.label("step_error"),
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
            "architect (author-no-diff): no run/task resolvable for step %s; skipping",
            step_id,
        )
        return None
    if row.workflow_id != AUTHOR_WORKFLOW_ID:
        return None

    if not row.step_error or _NO_CHANGES_ERROR_SIGNATURE not in row.step_error:
        return None

    if await _is_capped(session, row.task_id, ARCHITECTURE_RESOLVE_WORKFLOW_ID):
        logger.warning(
            "architect (author-no-diff): %s capped for task %s "
            "(>=%d prior runs); operator must intervene",
            ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            row.task_id,
            ARCHITECTURE_RESOLVE_MAX_ATTEMPTS,
        )
        # SDE-5: surface to operator. This cap site previously logged
        # "operator must intervene" but emitted nothing the needs_operator
        # query (GET /tasks?status=needs_operator) could see.
        await _emit_arch_cap_reached(
            session, dispatcher, task_id=row.task_id, repo=row.repo,
        )
        return None

    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    payload = {"repo": row.repo, "author_no_diff_run_id": str(row.run_id)}

    async def _dispatch() -> uuid.UUID | None:
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id=ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            trigger="self:wf-author-no-diff",
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id=ARCHITECTURE_RESOLVE_WORKFLOW_ID,
        payload=payload,
        dispatch_fn=_dispatch,
    )


async def maybe_dispatch_architect_on_author_remote_rejection(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    workflow_id: str,
) -> uuid.UUID | None:
    """Dispatch ``wf-architecture-resolve`` when a wf-author step.failed
    carries a remote-rejected push error (ADR-0048).

    When ``git push --force-with-lease`` is rejected — by GitHub branch
    protection, a stale lease from a concurrent writer, or any other
    remote-side guard — the step fails with stderr containing
    ``_REMOTE_REJECTION_ERROR_SIGNATURE``.  No PR was opened, so
    wf-feedback has nothing to remediate.  The architect reviews the
    situation and almost always verdicts ``supersede`` to close the
    rejected branch and start fresh.

    Skip conditions are identical to ``maybe_dispatch_architect_on_author_no_diff``:
      * ``workflow_id`` is not ``'wf-author'``.
      * The step's error doesn't contain ``_REMOTE_REJECTION_ERROR_SIGNATURE``.
      * Owning task can't be resolved.
      * No ``WorkflowVersion`` for ``wf-architecture-resolve`` (un-seeded).
      * Task already has >= ``ARCHITECTURE_RESOLVE_MAX_ATTEMPTS`` runs (cap).

    Dedup key:
        ``wf-architecture-resolve:<repo>:remote-rejected-run=<wf_author_run_id>``

    Distinct namespace from ``author-no-diff-run`` so both failure shapes
    can each fire one architecture-resolve without colliding.
    """
    if workflow_id != AUTHOR_WORKFLOW_ID:
        return None

    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.id.label("run_id"),
            WorkflowRun.task_id,
            Task.repo,
            WorkflowRunStep.error.label("step_error"),
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
            "architect (author-remote-rejection): no run/task resolvable for step %s; skipping",
            step_id,
        )
        return None
    if row.workflow_id != AUTHOR_WORKFLOW_ID:
        return None

    if not row.step_error or _REMOTE_REJECTION_ERROR_SIGNATURE not in row.step_error:
        return None

    if await _is_capped(session, row.task_id, ARCHITECTURE_RESOLVE_WORKFLOW_ID):
        logger.warning(
            "architect (author-remote-rejection): %s capped for task %s "
            "(>=%d prior runs); operator must intervene",
            ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            row.task_id,
            ARCHITECTURE_RESOLVE_MAX_ATTEMPTS,
        )
        # SDE-5: surface to operator (see author-no-diff cap site above).
        await _emit_arch_cap_reached(
            session, dispatcher, task_id=row.task_id, repo=row.repo,
        )
        return None

    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    payload = {"repo": row.repo, "author_remote_reject_run_id": str(row.run_id)}

    async def _dispatch() -> uuid.UUID | None:
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id=ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            trigger="self:wf-author-remote-rejection",
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id=ARCHITECTURE_RESOLVE_WORKFLOW_ID,
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


async def _emit_arch_cap_reached(
    session: AsyncSession,
    dispatcher: Any,
    *,
    task_id: uuid.UUID,
    repo: str,
) -> None:
    """Persist + publish task.escalated_to_operator when the arch-resolve cap fires.

    Queries the last completed wf-architecture-resolve step for its verdict
    and reasoning, and the IDs of recent runs, then emits the event via the
    dispatcher. If the dispatcher is absent (test stubs) or the task row is
    missing, logs and returns without raising — the cap warning above already
    recorded the signal.
    """
    if dispatcher is None:
        return
    task = await session.get(Task, task_id)
    if task is None:
        logger.warning(
            "arch-cap-reached: task %s not found; skipping escalation event",
            task_id,
        )
        return

    # Fetch last architect step output for verdict + reasoning.
    last_step_result = await session.execute(
        select(WorkflowRunStep.output)
        .join(WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
        .where(
            WorkflowRun.task_id == task_id,
            WorkflowVersion.workflow_id == ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            WorkflowRunStep.status == "completed",
        )
        .order_by(WorkflowRunStep.completed_at.desc().nulls_last())
        .limit(1)
    )
    last_step = last_step_result.first()
    last_verdict: str | None = None
    last_reasoning: str | None = None
    if last_step and last_step.output:
        arch_payload = (last_step.output.get("payload") or {})
        last_verdict = arch_payload.get("verdict")
        last_reasoning = arch_payload.get("reasoning")

    # Fetch recent run IDs (up to 5) for the operator's reference.
    run_ids_result = await session.execute(
        select(WorkflowRun.id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
        .where(
            WorkflowRun.task_id == task_id,
            WorkflowVersion.workflow_id == ARCHITECTURE_RESOLVE_WORKFLOW_ID,
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(5)
    )
    run_ids = [str(r) for r in run_ids_result.scalars()]

    try:
        await dispatcher.persist_and_publish(
            session,
            entity_type="task",
            action="escalated_to_operator",
            payload=TaskEscalatedToOperator(
                task_id=task_id,
                repo=repo,
                last_verdict=last_verdict,
                last_reasoning=last_reasoning,
                run_ids=run_ids,
                # ADR-0058: tag the escalation reason so the dashboard
                # can triage architect-cap fires separately from
                # gate-broken cases.
                reason="architect_cap",
                created_by=task.created_by,
            ),
            plan_id=task.plan_id,
            task_id=task_id,
        )
    except Exception:
        logger.exception(
            "arch-cap-reached: failed to emit escalation event for task %s; "
            "operator must check logs",
            task_id,
        )


# Recovery workflows whose own terminal give-up (decision=fail) or cap leaves
# the PR gate-blocked with no productive next dispatch. Per the 2026-05-19
# dead-end audit these must surface to operator instead of silently no-op'ing.
_TERMINAL_GIVE_UP_WORKFLOWS = frozenset(
    {CI_FIX_WORKFLOW_ID, CONFLICT_WORKFLOW_ID, DOC_AMEND_WORKFLOW_ID}
)


async def _emit_operator_escalation(
    session: AsyncSession,
    dispatcher: Any,
    *,
    task_id: uuid.UUID,
    signal: str,
    repo: str | None = None,
    detail: str | None = None,
    reason: Literal["architect_cap", "stuck_task_sweep", "gate-broken", "terminal_gate_sweep", "step_starvation"] | None = None,
) -> None:
    """Persist + publish ``task.escalated_to_operator`` for a non-architect
    terminal that has no productive next step (a cap or a give-up).

    ``signal`` is a short slug stored in ``last_verdict`` (e.g.
    ``'wf-ci-fix-cap-reached'``); ``detail`` goes to ``last_reasoning``.
    ``repo`` defaults to the task's own repo when not supplied. ``reason``
    (ADR-0058) tags the escalation source for dashboard triage; callers
    that don't supply it land as ``None`` (legacy escalations).
    The operator surface is ``GET /api/v1/tasks?status=needs_operator``
    (PR #184), which uses ``EXISTS`` on the escalation event. We still
    guard against duplicate rows for the same ``(task, signal)`` so
    reprocessing or repeated cap hits don't pile up escalations. No-op
    when the dispatcher is absent (test stubs) or the task row is
    missing — the caller's warning log already recorded the signal.
    """
    if dispatcher is None:
        return
    task = await session.get(Task, task_id)
    if task is None:
        logger.warning(
            "operator-escalation: task %s not found; skipping %s",
            task_id, signal,
        )
        return
    repo = repo or task.repo
    existing = await session.execute(
        select(Event.id)
        .where(
            Event.task_id == task_id,
            Event.entity_type == "task",
            Event.action == "escalated_to_operator",
            Event.payload["last_verdict"].astext == signal,
        )
        .limit(1)
    )
    if existing.first() is not None:
        return
    try:
        await dispatcher.persist_and_publish(
            session,
            entity_type="task",
            action="escalated_to_operator",
            payload=TaskEscalatedToOperator(
                task_id=task_id,
                repo=repo,
                last_verdict=signal,
                last_reasoning=detail,
                run_ids=[],
                reason=reason,
                created_by=task.created_by,
            ),
            plan_id=task.plan_id,
            task_id=task_id,
        )
    except Exception:
        logger.exception(
            "operator-escalation: failed to emit %s for task %s; "
            "operator must check logs",
            signal, task_id,
        )


async def maybe_escalate_operator_on_terminal_give_up(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
) -> None:
    """SDE-2/4b/6: surface to operator when a recovery workflow gives up.

    ``wf-ci-fix`` / ``wf-conflict`` / ``wf-doc-amend`` completing with
    ``decision='fail'`` has no productive downstream dispatch — the PR stays
    gate-blocked. Before the 2026-05-19 audit this terminated silently (no
    consumer handler keys on these workflows' own terminals). Emit
    ``task.escalated_to_operator`` so the needs_operator query finds it.
    Short-circuits cleanly for any other workflow or decision.
    """
    decision = typed.output.decision if typed.output else None
    if decision != "fail":
        return
    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.task_id,
            Task.repo,
        )
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
        .join(Task, Task.id == WorkflowRun.task_id)
        .where(WorkflowRunStep.id == step_id)
    )
    row = result.first()
    if row is None or row.workflow_id not in _TERMINAL_GIVE_UP_WORKFLOWS:
        return
    await _emit_operator_escalation(
        session,
        dispatcher,
        task_id=row.task_id,
        repo=row.repo,
        signal=f"{row.workflow_id}-gave-up",
        detail=(
            f"{row.workflow_id} completed with decision=fail and has no "
            "productive next step; operator intervention needed."
        ),
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
        await _emit_arch_cap_reached(session, dispatcher, task_id=row.task_id, repo=row.repo)
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


async def maybe_dispatch_architect_on_feedback_validation_fail(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """ADR-0048 follow-on: when wf-feedback's action step completes with
    ``decision='fail'`` because author-side deterministic validation
    (``runner_dispositions/code.py::_run_author_validations``) rejected
    the worker's diff, route directly to ``wf-architecture-resolve``.

    Predicate:
      * step belongs to ``wf-feedback``
      * ``step_name == 'action'``
      * ``output.decision == 'fail'``
      * ``output.payload.validation_results`` contains at least one
        verdict='fail' entry
      * task has no open PR (any ``task_prs`` row with
        ``closed_at IS NULL`` → skip and let the deadlock-arbitration
        path own PR-bearing cases)
      * existing cap on ``wf-architecture-resolve`` applies

    Sibling of ``maybe_dispatch_architect_on_author_no_diff`` (PR #187)
    for a different trigger source: PR #187 fires on wf-author's
    no-changes step.failed (no diff was produced at all); this fires
    on wf-feedback's action step.completed when a diff WAS produced
    but was rejected by deterministic author-side validation.

    **Diff handling note** (per ``runner_dispositions/code.py``): when
    author-side validation fails, the disposition has already executed
    ``git.commit_all`` (line 76) and produced a ``commit_sha`` on the
    LOCAL branch. The push is then skipped, so the commit lives only
    in the worker's repo_dir (which is torn down at step end). From
    the architect's perspective the diff is effectively discarded —
    only ``validation_results.log_excerpt`` and the worker's
    ``summary`` survive in the step's ``output`` payload.

    Dedup namespace:
        ``wf-architecture-resolve:<repo>:feedback-validation-fail-step=<wf_feedback_action_step_id>``

    Keyed on the STEP id (not run id) because wf-feedback's two-step
    structure (analyzer + action) means the same run id could in
    principle contain multiple action attempts; we want one architect
    dispatch per action-step that produced this shape.

    Returns the new ``wf-architecture-resolve`` run's id, or ``None``
    if any skip condition fired.

    Ordering invariant: callers MUST invoke this AFTER
    ``maybe_dispatch_arbitration_on_deadlock``. The deadlock trigger
    fires when a blocking gate signal exists (PR-bearing cases). When
    no PR exists there is no gate signal so the deadlock trigger
    short-circuits silently, leaving this trigger to pick up the
    no-PR validation-fail case. See ADR-0048 / consumer hazard note.
    """
    # Cheap filter: only fire on decision=fail. Other wf-feedback
    # decisions (``pushed``, ``responded-without-change``) are not
    # validation-rejection shapes.
    if typed.output.decision != "fail":
        return None

    # Defensive payload shape filter: the validation-rejection shape
    # carries a ``validation_results`` list with at least one entry
    # marked ``verdict='fail'``. Any other ``decision='fail'`` shape
    # (e.g. a feedback worker that crashed and surfaced as fail
    # without this payload) is out of scope — the deadlock trigger or
    # a future helper handles those.
    validation_results = (typed.output.payload or {}).get("validation_results")
    if not isinstance(validation_results, list) or not validation_results:
        return None
    if not any(
        isinstance(r, dict) and r.get("verdict") == "fail"
        for r in validation_results
    ):
        return None

    # Resolve workflow + run + task + repo + step_name for the
    # completing step. Workflow + step_name filter happens after the
    # cheap payload filter so we only hit the DB on candidate events.
    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.id.label("run_id"),
            WorkflowRun.task_id,
            Task.repo,
            WorkflowRunStep.step_name,
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
            "architect (feedback-validation-fail): no run/task resolvable "
            "for step %s; skipping",
            step_id,
        )
        return None
    if row.workflow_id != FEEDBACK_WORKFLOW_ID:
        return None
    if row.step_name != "action":
        # Only the action step's failure indicates a tried-and-rejected
        # diff. The analyzer step's decision=fail (if it ever happens)
        # means the analyzer couldn't even produce a directive — that
        # is not a validation-fail shape.
        return None

    # No-PR predicate: if any open PR exists for this task, let the
    # deadlock-arbitration path handle it. This trigger owns the
    # no-PR case specifically (which is what dead-ends 4 of 6 retries
    # in the 2026-05-19 batch).
    open_pr_result = await session.execute(
        select(TaskPR.pr_number)
        .where(
            TaskPR.task_id == row.task_id,
            TaskPR.closed_at.is_(None),
        )
        .limit(1)
    )
    if open_pr_result.first() is not None:
        logger.debug(
            "architect (feedback-validation-fail): task %s has an open PR; "
            "deferring to deadlock-arbitration trigger",
            row.task_id,
        )
        return None

    if await _is_capped(session, row.task_id, ARCHITECTURE_RESOLVE_WORKFLOW_ID):
        logger.warning(
            "architect (feedback-validation-fail): %s capped for task %s "
            "(>=%d prior runs); operator must intervene",
            ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            row.task_id,
            ARCHITECTURE_RESOLVE_MAX_ATTEMPTS,
        )
        await _emit_arch_cap_reached(
            session, dispatcher, task_id=row.task_id, repo=row.repo,
        )
        return None

    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    payload = {
        "repo": row.repo,
        "feedback_validation_fail_step_id": str(step_id),
    }

    async def _dispatch() -> uuid.UUID | None:
        # Pass the wf-feedback action step's id as ``source_step_id``
        # so the architect's worker can read the step's ``output``
        # payload (including ``validation_results`` with check_ids and
        # rationales) via the SourceStep block — same plumbing as
        # PR #190's architect→feedback path. The architect needs this
        # to understand WHY the diff was rejected.
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id=ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            trigger="self:wf-feedback-validation-fail",
            source_step_id=step_id,
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id=ARCHITECTURE_RESOLVE_WORKFLOW_ID,
        payload=payload,
        dispatch_fn=_dispatch,
    )


async def maybe_dispatch_architect_on_feedback_no_progress(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """Dead-end audit (2026-05-19) SDE-1: route a no-progress wf-feedback
    terminal on a no-PR task to ``wf-architecture-resolve``.

    Sibling of ``maybe_dispatch_architect_on_feedback_validation_fail``. That
    helper owns the ``decision='fail'`` + ``validation_results`` shape (a diff
    was produced and rejected by author-side validation). This helper owns the
    *other* no-progress shapes that otherwise terminate silently when no PR
    exists and no blocking gate signal is present:

      * ``decision='responded-without-change'`` — the feedback worker decided
        nothing should change. With a blocking gate this is the deadlock path
        (``maybe_dispatch_arbitration_on_deadlock``); with no PR there is no
        gate, so that path short-circuits and this one picks it up.
      * ``decision='fail'`` WITHOUT a ``validation_results`` fail entry — e.g.
        a feedback worker that failed for some other reason and produced no
        diff (the retry-CLI path that re-dispatches wf-feedback on a task that
        never had a PR).

    Predicate (mirrors the validation-fail sibling):
      * step belongs to ``wf-feedback``; ``step_name == 'action'``
      * decision is ``responded-without-change`` OR a bare ``fail`` (a ``fail``
        carrying a ``validation_results`` fail entry is the sibling's)
      * task has no open PR (PR-bearing cases stay on the deadlock path)
      * the ``wf-architecture-resolve`` cap applies (surfaces to operator)

    Dedup namespace:
        ``wf-architecture-resolve:<repo>:feedback-no-progress-step=<step_id>``

    Ordering invariant: callers MUST invoke this AFTER both
    ``maybe_dispatch_arbitration_on_deadlock`` and
    ``maybe_dispatch_architect_on_feedback_validation_fail`` so the
    gate-bearing and validation-fail cases are claimed first.
    """
    decision = typed.output.decision if typed.output else None
    if decision not in ("responded-without-change", "fail"):
        return None
    # A decision=fail carrying a validation_results fail entry is owned by
    # maybe_dispatch_architect_on_feedback_validation_fail — skip it here.
    if decision == "fail":
        validation_results = (typed.output.payload or {}).get("validation_results")
        if isinstance(validation_results, list) and any(
            isinstance(r, dict) and r.get("verdict") == "fail"
            for r in validation_results
        ):
            return None

    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
            WorkflowRun.id.label("run_id"),
            WorkflowRun.task_id,
            Task.repo,
            WorkflowRunStep.step_name,
        )
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
        .join(Task, Task.id == WorkflowRun.task_id)
        .where(WorkflowRunStep.id == step_id)
    )
    row = result.first()
    if row is None:
        return None
    if row.workflow_id != FEEDBACK_WORKFLOW_ID or row.step_name != "action":
        return None

    open_pr_result = await session.execute(
        select(TaskPR.pr_number)
        .where(TaskPR.task_id == row.task_id, TaskPR.closed_at.is_(None))
        .limit(1)
    )
    if open_pr_result.first() is not None:
        logger.debug(
            "architect (feedback-no-progress): task %s has an open PR; "
            "deferring to the deadlock-arbitration trigger",
            row.task_id,
        )
        return None

    if await _is_capped(session, row.task_id, ARCHITECTURE_RESOLVE_WORKFLOW_ID):
        logger.warning(
            "architect (feedback-no-progress): %s capped for task %s "
            "(>=%d prior runs); operator must intervene",
            ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            row.task_id,
            ARCHITECTURE_RESOLVE_MAX_ATTEMPTS,
        )
        await _emit_arch_cap_reached(
            session, dispatcher, task_id=row.task_id, repo=row.repo,
        )
        return None

    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    payload = {
        "repo": row.repo,
        "feedback_no_progress_step_id": str(step_id),
    }

    async def _dispatch() -> uuid.UUID | None:
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id=ARCHITECTURE_RESOLVE_WORKFLOW_ID,
            trigger="self:wf-feedback-no-progress",
            source_step_id=step_id,
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
        # Pass the architect's step_id as ``source_step_id`` so the
        # downstream wf-feedback worker can read the architect's
        # ``remediation_summary`` + ``reasoning`` from this step's
        # ``output`` (JSONB per ADR-0011) on its initial context
        # fetch. Without this plumbing the architect's directive is
        # dropped at the dispatch boundary and the analyzer re-evaluates
        # from scratch, often re-concluding ``no code change needed``
        # against the architect's explicit directive (observed
        # 2026-05-16 on PRs #120/#122/#123/#124).
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id=FEEDBACK_WORKFLOW_ID,
            trigger="self:architect-amend",
            source_step_id=step_id,
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id=FEEDBACK_WORKFLOW_ID,
        payload=payload,
        dispatch_fn=_dispatch,
    )


_REVIEW_OVERRIDE_NAMESPACE = uuid.UUID("8e16f4c0-2c4c-4f4f-9b9a-7c3d6f1a5e10")
_VALIDATE_OVERRIDE_NAMESPACE = uuid.UUID("2f7c8a3d-5e9b-4c1d-a2e7-3b5f9d6e0a14")


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


async def maybe_emit_validate_override_on_architect_completion(
    session: AsyncSession,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """ADR-0042: emit a ``validate.override`` Event row when an architect
    step.completed carries ``payload.dispatch.validate_override == True``.

    Sibling to ``maybe_emit_review_override_on_architect_completion``.
    The mergeability VIEW (post-20260518_1715 migration) reads
    ``validate.override`` events at HEAD as ``validate_decision='pass'``,
    unblocking auto-merge for validate-fail-driven deadlocks the architect
    resolved with ``verdict='accept-as-is'``.

    The architect emits both ``review_override`` and ``validate_override``
    flags on every deadlock ``accept-as-is`` (see
    ``runner_dispositions/architecture.py``). Each override only matters
    in the VIEW if its corresponding gate's latest signal at HEAD was a
    fail; an override against an already-passing gate is harmless because
    the UNION ALL in the validate LATERAL prefers the most-recent signal
    by timestamp, and an override emitted at the same instant the gate
    was already pass-ing leaves the pre-existing pass in place.

    Idempotency: ``id`` is a deterministic UUIDv5 of
    ``(task_id, commit_sha)`` under a distinct namespace from
    ``review.override`` so the two overrides co-exist for the same
    architect step.

    Returns the emitted Event id, or ``None`` if no emission happened.
    """
    output = typed.output
    payload = output.payload if isinstance(output.payload, dict) else {}
    dispatch = payload.get("dispatch") if isinstance(payload, dict) else None
    if not isinstance(dispatch, dict) or not dispatch.get("validate_override"):
        return None

    # Confirm this step belongs to wf-architecture-resolve. Any other
    # workflow setting ``validate_override`` is malformed; skip defensively.
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
            "validate_override emission skipped: step %s is not "
            "wf-architecture-resolve (workflow=%s)",
            step_id, row.workflow_id if row else None,
        )
        return None

    pr_result = await session.execute(
        select(TaskPR.repo, TaskPR.pr_number).where(
            TaskPR.task_id == row.task_id,
        )
    )
    pr_row = pr_result.first()
    if pr_row is None:
        logger.warning(
            "validate_override emission skipped: no task_pr for task %s",
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
            "validate_override emission skipped: no pr_opened/pr_synchronize "
            "event for task %s (repo=%s pr=%s)",
            row.task_id, pr_row.repo, pr_row.pr_number,
        )
        return None
    head_sha = head_row.payload["head_sha"]

    reasoning = payload.get("reasoning") or ""

    event_id = uuid.uuid5(
        _VALIDATE_OVERRIDE_NAMESPACE,
        f"{row.task_id}:{head_sha}",
    )
    stmt = (
        pg_insert(Event)
        .values(
            id=event_id,
            entity_type="validate",
            action="override",
            task_id=row.task_id,
            commit_sha=head_sha,
            payload={
                "commit_sha": head_sha,
                "reasoning": reasoning,
                # Empty list = blanket "all failing checks at this sha".
                # Future ADR-0040 tuning emission can populate this list
                # to narrow the override to named check_ids.
                "override_validate_check_ids": [],
            },
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.execute(stmt)
    logger.info(
        "validate.override emitted: task=%s commit_sha=%s event_id=%s",
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


async def maybe_dispatch_rule_tuning_on_architect_completion(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """ADR-0040: fire ``wf-doc-amend`` when a ``wf-architecture-resolve``
    step.completed carries ``payload.validator_tuning``.

    The architect emits a ``ValidatorTuning`` proposal when its verdict is
    ``accept-as-is`` and the blocking signal was a validator ``fail`` (not a
    reviewer ``changes_requested``).  This trigger picks it up and routes a
    doc-amend run so the documentarian can apply the proposed rule-YAML edit
    under operator review.

    Intent literal: ``tune-rule-from-architect``.

    Skips cleanly when:
      * ``payload.validator_tuning`` is absent (common path — most architect
        completions don't carry tuning proposals).
      * The tuning payload is malformed (logged at WARNING + skipped).
      * The step doesn't belong to ``wf-architecture-resolve``.
      * Task can't be resolved (deleted between dispatch + completion).
      * No ``WorkflowVersion`` exists for ``wf-doc-amend`` (un-seeded).
      * Task has already dispatched ``wf-doc-amend`` >= ``DOC_AMEND_MAX_ATTEMPTS``
        times (cap).

    Dedup namespace: ``wf-doc-amend:<repo>:tune-rule=<rule_slug>`` — at most
    one tuning doc-amend per (repo, rule) regardless of re-delivery.

    Returns the new ``wf-doc-amend`` run's id, or ``None`` if any skip
    condition fired.
    """
    from treadmill_api.events.validator_tuning import ValidatorTuning

    output = typed.output
    raw_payload = output.payload if isinstance(output.payload, dict) else {}
    tuning_raw = raw_payload.get("validator_tuning")
    if not tuning_raw:
        return None

    try:
        tuning = ValidatorTuning.model_validate(tuning_raw)
    except Exception:
        logger.warning(
            "rule-tuning trigger: malformed validator_tuning in step %s; "
            "dropping (will not dispatch wf-doc-amend)",
            step_id,
        )
        return None

    # Confirm this step belongs to wf-architecture-resolve.
    result = await session.execute(
        select(
            WorkflowVersion.workflow_id,
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
        logger.debug(
            "rule-tuning trigger: step %s is not wf-architecture-resolve "
            "(workflow=%s); skipping",
            step_id, row.workflow_id if row else None,
        )
        return None

    if await _is_capped(session, row.task_id, DOC_AMEND_WORKFLOW_ID):
        logger.warning(
            "rule-tuning trigger: %s capped for task %s (>=%d prior runs); "
            "skipping",
            DOC_AMEND_WORKFLOW_ID, row.task_id, DOC_AMEND_MAX_ATTEMPTS,
        )
        return None

    task = await session.get(Task, row.task_id)
    if task is None:
        return None

    dedup_payload = {
        "repo": row.repo,
        "rule_slug": tuning.rule_slug,
        "intent": "tune-rule-from-architect",
        "tuning_proposal": tuning.model_dump(),
    }

    async def _dispatch() -> uuid.UUID | None:
        return await _create_and_publish_run(
            session,
            dispatcher,
            task=task,
            workflow_id=DOC_AMEND_WORKFLOW_ID,
            trigger="self:wf-architecture-tune-rule",
        )

    return await maybe_dispatch_with_dedup(
        session,
        workflow_id=DOC_AMEND_WORKFLOW_ID,
        payload=dedup_payload,
        dispatch_fn=_dispatch,
    )


async def maybe_dispatch_supersede_on_architect_verdict(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
    github_client: Any = None,
) -> uuid.UUID | None:
    """ADR-0048: handle the architect's repurposed ``supersede`` verdict.

    When the architect verdicts ``supersede`` the plan-text itself was
    wrong. We close the existing PR (if any), create a CHILD task row
    carrying the architect's ``rewritten_description`` (with
    ``parent_task_id`` pointing back to the original), and dispatch a
    fresh ``wf-author`` run against the child. Task text remains
    immutable per row — supersede creates a new row, not an in-place
    edit.

    Predicate: the step belongs to ``wf-architecture-resolve`` AND its
    output payload carries ``verdict='supersede'`` with a non-empty
    ``rewritten_description``. The worker-side parse (and the
    ``ArchitectVerdict`` Pydantic validator) already reject empty-rewrite
    supersedes, so this trigger defensively re-checks rather than
    failing silently.

    Side-effect order (most-recoverable last):

      1. Resolve owning task + repo + (optionally) the open PR.
      2. Create the child task row with the rewritten description.
      3. Dispatch ``wf-author`` against the child (via
         ``_create_and_publish_run``).
      4. Best-effort close the parent's PR. PR-close failures are
         logged and swallowed — the child task is already created and
         dispatched, so the new work proceeds even if GitHub is
         temporarily unreachable. The operator can close the stale PR
         manually if needed.

    Dedup key (per ADR-0026): ``wf-author:<repo>:supersede-parent=<parent_task_id>``
    — re-delivery of the same architect step.completed cannot create N
    children against the same parent.

    Returns the new wf-author run's id, or ``None`` if any skip
    condition fired (non-supersede payload, non-architect step,
    missing rewrite text, no workflow version seeded, dedup collision).
    """
    output = typed.output
    payload = output.payload if isinstance(output.payload, dict) else {}
    if not isinstance(payload, dict):
        return None
    verdict = payload.get("verdict")
    if verdict != "supersede":
        return None

    # Pull the rewritten description from either the top-level field
    # (where the worker disposition surfaces it) or the dispatch
    # sub-object (where it also appears for downstream readers). Top-
    # level wins.
    rewritten = payload.get("rewritten_description")
    if not rewritten:
        dispatch = payload.get("dispatch")
        if isinstance(dispatch, dict):
            rewritten = dispatch.get("rewritten_description")
    if not isinstance(rewritten, str) or not rewritten.strip():
        logger.warning(
            "supersede trigger: step %s carries verdict=supersede but no "
            "rewritten_description; skipping (worker-side parse should "
            "have rejected this — defensive)",
            step_id,
        )
        return None

    # Confirm this step belongs to wf-architecture-resolve.
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
        logger.debug(
            "supersede trigger: step %s is not wf-architecture-resolve "
            "(workflow=%s); skipping",
            step_id, row.workflow_id if row else None,
        )
        return None

    parent_task = await session.get(Task, row.task_id)
    if parent_task is None:
        logger.warning(
            "supersede trigger: parent task %s not found; skipping",
            row.task_id,
        )
        return None

    reasoning = payload.get("reasoning") or ""

    # The dedup key keys on the PARENT task id — re-delivery of the
    # same architect step.completed must not create N children against
    # the same parent. The child's id is freshly generated below; we
    # dispatch first under the dedup gate, then close the parent PR
    # as a follow-up best-effort step (so a transient PR-close failure
    # doesn't block forward progress).
    dedup_payload = {
        "repo": parent_task.repo,
        "supersede_parent_task_id": str(parent_task.id),
    }

    # Holder so the closure can pass the child task id back to the
    # caller for the PR-close step.
    child_holder: dict[str, Any] = {"child_task": None}

    async def _dispatch() -> uuid.UUID | None:
        # Create the child task row. Inherits ``plan_id``,
        # ``workflow_version_id``, ``repo`` from the parent; carries the
        # rewritten ``description`` and ``parent_task_id`` pointing back.
        # Title is suffixed with " (superseded)" so the operator UI can
        # disambiguate parent vs child at a glance.
        child_title = parent_task.title
        if not child_title.endswith("(superseded)"):
            child_title = f"{parent_task.title} (superseded)"
        child = Task(
            plan_id=parent_task.plan_id,
            repo=parent_task.repo,
            title=child_title,
            description=rewritten,
            workflow_version_id=parent_task.workflow_version_id,
            created_by="architect:supersede",
            parent_task_id=parent_task.id,
        )
        session.add(child)
        await session.flush()
        child_holder["child_task"] = child

        return await _create_and_publish_run(
            session,
            dispatcher,
            task=child,
            workflow_id=AUTHOR_WORKFLOW_ID,
            trigger="self:wf-architecture-supersede",
        )

    run_id = await maybe_dispatch_with_dedup(
        session,
        workflow_id=AUTHOR_WORKFLOW_ID,
        payload=dedup_payload,
        dispatch_fn=_dispatch,
    )

    if run_id is None:
        # Dedup collision OR _create_and_publish_run found no workflow
        # version. In either case the supersede side-effect is complete
        # (either a prior delivery already won, or the install can't
        # dispatch wf-author at all). Nothing more to do.
        return None

    # Best-effort PR close. Done AFTER the child is created so a
    # transient GitHub failure doesn't lose the supersede signal.
    child_task = child_holder.get("child_task")
    await _maybe_close_parent_pr_for_supersede(
        session,
        github_client=github_client,
        parent_task_id=parent_task.id,
        repo=parent_task.repo,
        child_task_id=child_task.id if child_task is not None else None,
        reasoning=reasoning,
    )

    return run_id


# ── ADR-0058: gate-broken escalation ─────────────────────────────────────────


async def maybe_dispatch_gate_broken_escalation(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
    typed: StepCompleted,
) -> uuid.UUID | None:
    """ADR-0058: handle the architect's ``gate-broken`` verdict.

    Predicate: the step belongs to ``wf-architecture-resolve`` AND its
    output payload carries ``verdict='gate-broken'`` with a non-empty
    ``gate_log_excerpt``. The worker-side parse (and the
    ``ArchitectVerdict`` Pydantic validator) already reject excerpt-
    less gate-broken, so this trigger defensively re-checks rather
    than failing silently.

    Effect: emit ``task.escalated_to_operator`` with
    ``reason='gate-broken'`` and the gate's stderr captured. The
    amend-cap counter (per ADR-0029 Q29.e) is NOT incremented — that
    counter fires on consecutive architect-amend dispatches; a
    gate-broken verdict isn't amend, so the counter naturally doesn't
    advance. No new ``WorkflowRun`` is materialized.

    Idempotency: an explicit dedup is unnecessary because the
    ``escalated_to_operator`` event is keyed by the same
    ``step.completed`` (one event per architect step). If SQS
    redelivers the same step.completed, the prior projection will
    have already committed the escalation; this fires again and
    creates a second escalation row — which the dashboard's
    `_ESCALATIONS_SQL` "latest-with-no-ack" filter de-duplicates
    cleanly. The cost of double-emit is low; the cost of a missed
    emit is operator-visible.

    Returns the new ``task.escalated_to_operator`` event id on
    success, or ``None`` on any skip condition.
    """
    if dispatcher is None:
        return None
    output = typed.output
    payload = output.payload if isinstance(output.payload, dict) else {}
    if not isinstance(payload, dict):
        return None
    if payload.get("verdict") != "gate-broken":
        return None
    gate_log_excerpt = payload.get("gate_log_excerpt") or ""
    if not gate_log_excerpt.strip():
        logger.warning(
            "gate-broken dispatch: step %s carries verdict=gate-broken "
            "but no gate_log_excerpt; skipping escalation (architect "
            "should re-emit with excerpt populated)",
            step_id,
        )
        return None

    # Confirm the step belongs to wf-architecture-resolve. We could
    # accept gate-broken from other architect-bearing workflows in
    # principle, but today the only architect dispatcher is
    # wf-architecture-resolve. Refusing other workflows here makes
    # the surface single-sourced.
    step_uuid = uuid.UUID(step_id) if isinstance(step_id, str) else step_id
    workflow_id_result = await session.execute(
        select(WorkflowVersion.workflow_id)
        .join(WorkflowRun, WorkflowRun.workflow_version_id == WorkflowVersion.id)
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .where(WorkflowRunStep.id == step_uuid)
        .limit(1)
    )
    wf_id = workflow_id_result.scalar_one_or_none()
    if wf_id != ARCHITECTURE_RESOLVE_WORKFLOW_ID:
        logger.debug(
            "gate-broken dispatch: step %s is not wf-architecture-resolve "
            "(workflow=%s); skipping",
            step_id, wf_id,
        )
        return None

    # Resolve owning task + repo.
    task_result = await session.execute(
        select(Task)
        .join(WorkflowRun, WorkflowRun.task_id == Task.id)
        .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
        .where(WorkflowRunStep.id == step_uuid)
        .limit(1)
    )
    task = task_result.scalar_one_or_none()
    if task is None:
        logger.warning(
            "gate-broken dispatch: step %s has no owning task; skipping",
            step_id,
        )
        return None

    # Pull last_verdict / last_reasoning from this same step's payload
    # so the escalation event carries the architect's full context.
    last_reasoning = payload.get("reasoning")

    # Fetch recent architect run IDs for the operator's reference.
    run_ids_result = await session.execute(
        select(WorkflowRun.id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
        .where(
            WorkflowRun.task_id == task.id,
            WorkflowVersion.workflow_id == ARCHITECTURE_RESOLVE_WORKFLOW_ID,
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(5)
    )
    run_ids = [str(r) for r in run_ids_result.scalars()]

    try:
        event = await dispatcher.persist_and_publish(
            session,
            entity_type="task",
            action="escalated_to_operator",
            payload=TaskEscalatedToOperator(
                task_id=task.id,
                repo=task.repo,
                last_verdict="gate-broken",
                last_reasoning=last_reasoning,
                run_ids=run_ids,
                reason="gate-broken",
                gate_log_excerpt=gate_log_excerpt[:4000],
                created_by=task.created_by,
            ),
            plan_id=task.plan_id,
            task_id=task.id,
        )
    except Exception:
        logger.exception(
            "gate-broken dispatch: failed to emit escalation event for "
            "task %s (step %s); operator must check logs",
            task.id, step_id,
        )
        return None

    logger.info(
        "gate-broken dispatch: task %s escalated to operator "
        "(reason=gate-broken, step=%s, runs=%d)",
        task.id, step_id, len(run_ids),
    )
    return event.id if event is not None else None


# ADR-0062: window inside which a sibling ``task.escalated_to_operator``
# event (e.g. wf-conflict-cap-reached, wf-ci-fix-cap-reached) is treated
# as having already covered the same step.failed signal. Keeps the
# terminal-step-failure producer from double-emitting on cases the
# cap-reached path already raised.
_TERMINAL_STEP_FAILURE_DEDUP_WINDOW = timedelta(minutes=5)


async def maybe_dispatch_terminal_step_failure_escalation(
    session: AsyncSession,
    dispatcher: Any,
    *,
    step_id: str,
) -> uuid.UUID | None:
    """ADR-0062 Step 1: escalate to operator when a ``step.failed`` lands
    on a workflow run that has no remaining steps to dispatch and no
    sibling escalation has already fired for the task.

    Predicate (all must hold):
      * The terminating step exists and belongs to a resolvable task.
      * The run that owns the step has no remaining ``pending`` step
        with a higher ``step_index`` than the failing step (i.e. the
        cross-step orchestrator will not advance the run further).
      * No ``task.escalated_to_operator`` event has fired against the
        same task within the last
        ``_TERMINAL_STEP_FAILURE_DEDUP_WINDOW``. This is the dedup
        seam with the cap-reached producers (``_emit_arch_cap_reached``,
        ``_emit_operator_escalation``): if either has already raised an
        escalation for this task, the cap-reached event already
        captured the operator-visible signal.

    Effect: emit ``task.escalated_to_operator`` with
    ``reason='terminal_step_failure'``, ``step_name=<failing step>``,
    and ``gate_log_excerpt`` populated from the step row's captured
    ``error`` (when present and non-empty) so the operator sees the
    proximate failure on the escalation event without re-running the
    loop. The amend-cap counter is NOT touched — a terminal step
    failure isn't an architect verdict.

    Skip conditions return ``None``:
      * ``dispatcher`` is None (test stubs / narrow tests).
      * The step row can't be resolved (deleted between dispatch +
        terminal projection).
      * The owning task can't be resolved.
      * The run has at least one pending step past the failing step
        (cross-step orchestrator will advance; the loop will retry and
        a later terminal will own the escalation).
      * A recent sibling ``task.escalated_to_operator`` exists within
        the dedup window.

    Returns the new ``task.escalated_to_operator`` event id on
    success, or ``None`` on any skip condition.
    """
    if dispatcher is None:
        return None
    try:
        step_uuid = uuid.UUID(str(step_id))
    except (ValueError, TypeError):
        logger.warning(
            "terminal-step-failure dispatch: malformed step_id %r; skipping",
            step_id,
        )
        return None

    # Resolve the step row + the owning task + repo in one join. The
    # step row carries the ``error`` text the consumer just persisted
    # via the StepFailed projection, plus the ``step_name`` we surface
    # on the escalation payload.
    step_row_result = await session.execute(
        select(
            WorkflowRunStep.id.label("step_id"),
            WorkflowRunStep.run_id,
            WorkflowRunStep.step_index,
            WorkflowRunStep.step_name,
            WorkflowRunStep.error.label("step_error"),
            Task.id.label("task_id"),
            Task.repo,
            Task.plan_id,
            Task.created_by,
        )
        .join(WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id)
        .join(Task, Task.id == WorkflowRun.task_id)
        .where(WorkflowRunStep.id == step_uuid)
        .limit(1)
    )
    step_row = step_row_result.first()
    if step_row is None:
        logger.debug(
            "terminal-step-failure dispatch: step %s has no resolvable "
            "task; skipping",
            step_id,
        )
        return None

    # Predicate: does the owning run have any pending step with a
    # higher index than the failing step? If yes, the cross-step
    # orchestrator will advance and we let the next terminal own the
    # escalation decision. ``pending`` is the pre-dispatch status the
    # dispatcher writes at run-creation time (per
    # ``cross_step._find_next_pending_step``).
    pending_result = await session.execute(
        select(func.count())
        .select_from(WorkflowRunStep)
        .where(
            WorkflowRunStep.run_id == step_row.run_id,
            WorkflowRunStep.step_index > step_row.step_index,
            WorkflowRunStep.status == "pending",
        )
    )
    remaining_pending = pending_result.scalar_one() or 0
    if remaining_pending > 0:
        logger.debug(
            "terminal-step-failure dispatch: run %s has %d pending step(s) "
            "past index %d; skipping (cross-step loop will advance)",
            step_row.run_id, remaining_pending, step_row.step_index,
        )
        return None

    # Dedup window: another escalation event for the same task within
    # the last 5 minutes (the cap-reached producers) covers this case.
    cutoff = datetime.now(timezone.utc) - _TERMINAL_STEP_FAILURE_DEDUP_WINDOW
    recent_escalation_result = await session.execute(
        select(Event.id)
        .where(
            Event.task_id == step_row.task_id,
            Event.entity_type == "task",
            Event.action == "escalated_to_operator",
            Event.created_at >= cutoff,
        )
        .limit(1)
    )
    if recent_escalation_result.first() is not None:
        logger.debug(
            "terminal-step-failure dispatch: task %s already has a recent "
            "escalation event within the dedup window; skipping",
            step_row.task_id,
        )
        return None

    # Source the gate_log_excerpt from the step row's captured ``error``
    # column (the projection writer for ``step.failed`` persists
    # ``StepFailed.error`` here). Strip + cap at 4000 chars to match
    # the field's validator bound.
    raw_excerpt = (step_row.step_error or "").strip()
    gate_log_excerpt: str | None = raw_excerpt[:4000] if raw_excerpt else None

    try:
        event = await dispatcher.persist_and_publish(
            session,
            entity_type="task",
            action="escalated_to_operator",
            payload=TaskEscalatedToOperator(
                task_id=step_row.task_id,
                repo=step_row.repo,
                last_verdict=None,
                last_reasoning=None,
                run_ids=[str(step_row.run_id)],
                reason="terminal_step_failure",
                gate_log_excerpt=gate_log_excerpt,
                step_name=step_row.step_name,
                created_by=step_row.created_by,
            ),
            plan_id=step_row.plan_id,
            task_id=step_row.task_id,
        )
    except Exception:
        logger.exception(
            "terminal-step-failure dispatch: failed to emit escalation "
            "event for task %s (step %s); operator must check logs",
            step_row.task_id, step_id,
        )
        return None

    logger.info(
        "terminal-step-failure dispatch: task %s escalated to operator "
        "(reason=terminal_step_failure, step=%s, step_name=%s)",
        step_row.task_id, step_id, step_row.step_name,
    )
    return event.id if event is not None else None


async def _maybe_close_parent_pr_for_supersede(
    session: AsyncSession,
    *,
    github_client: Any,
    parent_task_id: uuid.UUID,
    repo: str,
    child_task_id: uuid.UUID | None,
    reasoning: str,
) -> None:
    """Best-effort: close the parent task's open PR (if any) and post a
    comment naming the child task.

    The supersede side-effect is fundamentally about the child task
    becoming the new home for the work — the stale PR is a follow-up
    cleanup. We never let a PR-close failure block the child-task
    creation or dispatch; on any error we log and return so the next
    redelivery / operator nudge can retry the close.

    Skip conditions (logged at DEBUG, not WARNING):
      * ``github_client`` is None — common in unit tests that don't
        exercise the real PR-close path.
      * No ``task_prs`` row for the parent — parent never opened a PR.
      * ``closed_at`` already set — PR already closed.
    """
    pr_result = await session.execute(
        select(TaskPR.pr_number, TaskPR.closed_at).where(
            TaskPR.task_id == parent_task_id,
        )
    )
    pr_row = pr_result.first()
    if pr_row is None:
        logger.debug(
            "supersede trigger: parent task %s has no task_prs row; "
            "no PR to close",
            parent_task_id,
        )
        return
    if pr_row.closed_at is not None:
        logger.debug(
            "supersede trigger: parent task %s PR #%s already closed; "
            "no-op",
            parent_task_id, pr_row.pr_number,
        )
        return

    if github_client is None:
        logger.debug(
            "supersede trigger: github_client unavailable; deferring PR "
            "close for parent task %s PR #%s (child task %s created + "
            "dispatched; operator can close stale PR manually)",
            parent_task_id, pr_row.pr_number, child_task_id,
        )
        return

    pr_number = pr_row.pr_number
    child_ref = f"task {child_task_id}" if child_task_id else "a child task"
    comment_body = (
        f"Superseded by {child_ref} — the architect verdicted "
        f"`supersede`, so the task text has been rewritten and a fresh "
        f"`wf-author` run is dispatching against the child task. "
        f"Closing this PR.\n\n"
        f"Architect reasoning: {reasoning or '(none provided)'}"
    )

    # Comment first (so the close has context), then close. Each step
    # is independent; either failure leaves the other side intact.
    try:
        comment_resp = await github_client.post(
            f"/repos/{repo}/issues/{pr_number}/comments",
            json={"body": comment_body},
        )
        comment_resp.raise_for_status()
    except Exception:
        logger.exception(
            "supersede trigger: failed to post supersede comment on "
            "PR #%s of %s (parent task %s); will still attempt close",
            pr_number, repo, parent_task_id,
        )

    try:
        close_resp = await github_client.patch(
            f"/repos/{repo}/pulls/{pr_number}",
            json={"state": "closed"},
        )
        close_resp.raise_for_status()
    except Exception:
        logger.exception(
            "supersede trigger: failed to close PR #%s of %s (parent "
            "task %s); child task %s is created and dispatched, operator "
            "can close stale PR manually",
            pr_number, repo, parent_task_id, child_task_id,
        )
        return

    # Mark the task_prs row as closed locally so subsequent redeliveries
    # see ``closed_at`` set and short-circuit cleanly. ``GithubPrMerged`` /
    # ``pr_closed`` webhook handlers usually do this, but those events
    # arrive asynchronously and may race with another supersede
    # redelivery.
    await session.execute(
        text(
            "UPDATE task_prs SET closed_at = now() "
            "WHERE task_id = :task_id AND pr_number = :pr_number "
            "  AND closed_at IS NULL"
        ),
        {"task_id": parent_task_id, "pr_number": pr_number},
    )
    logger.info(
        "supersede trigger: closed parent PR #%s of %s "
        "(parent task %s → child task %s)",
        pr_number, repo, parent_task_id, child_task_id,
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
        # SDE-6: docs-current-with-pr.fail → doc-amend at cap leaves the docs
        # gate failed → PR gate-blocked with no further recovery. Surface to
        # operator. (The rule-tuning doc-amend path produces a separate
        # proposal PR and is not gate-blocking, so it does not escalate.)
        await _emit_operator_escalation(
            session,
            dispatcher,
            task_id=row.task_id,
            signal=f"{DOC_AMEND_WORKFLOW_ID}-cap-reached",
            detail=(
                f"{DOC_AMEND_WORKFLOW_ID} reached its {DOC_AMEND_MAX_ATTEMPTS}-"
                "attempt cap on a docs-current gate failure; the PR stays "
                "gate-blocked. Operator intervention needed."
            ),
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
    source_step_id: uuid.UUID | str | None = None,
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

    ``source_step_id`` is the optional FK back to the upstream
    ``workflow_run_steps`` row whose ``step.completed`` triggered this
    dispatch. Self-trigger paths that need to plumb the upstream step's
    output into the downstream worker's context (e.g.
    ``maybe_dispatch_feedback_on_architect_amend`` plumbing the
    architect's ``remediation_summary``) pass it; the steps router
    joins through it on context fetch. Webhook fan-out, the initial
    ``wf-author`` dispatch, the deadlock-arbitration trigger, and the
    other paths that don't need cross-run plumbing leave it ``None``
    and the column stays ``NULL``. Per ADR-0011 this is the
    structured-column path; the source step's payload lives on
    ``workflow_run_steps.output`` (the one JSONB column the architecture
    commits to) — no new JSONB column on ``workflow_runs``.

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

    # Create the WorkflowRun. ``source_step_id`` is normalized to UUID
    # at the boundary — callers may pass either ``str`` (the SQS
    # message body shape) or ``uuid.UUID`` (the in-process consumer
    # call shape).
    coerced_source: uuid.UUID | None
    if source_step_id is None:
        coerced_source = None
    elif isinstance(source_step_id, uuid.UUID):
        coerced_source = source_step_id
    else:
        coerced_source = uuid.UUID(source_step_id)
    run = WorkflowRun(
        task_id=task.id,
        workflow_version_id=wv.id,
        trigger=trigger,
        source_step_id=coerced_source,
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

    Per ADR-0057, scheduled dispatch creates a **synthetic Task** per
    tick (tied to the system Plan, ``SYSTEM_PLAN_ID``) and runs the
    normal ``dispatcher.dispatch_task`` path — same code as a user-
    created task. This closes the silent-failure hole where the taskless
    path (``_create_and_publish_run_without_task``) sent ``task_id=null``
    to workers and they died silently in ``_handle_step`` before any
    event landed. Worker code is unchanged; tasks look normal to it.

    One slug — ``wf-stuck-task-sweep`` — short-circuits the
    ``WorkflowVersion`` lookup and runs a deterministic detector
    (``stuck_task_sweep.run_stuck_task_sweep``) instead. Per ADR-0047 the
    silent-stall signal is a pure query, no role step needed; intercepting
    here keeps the sweep on the same scheduler primitive that drives the
    role-step bots without forcing it through a one-step wrapper workflow.

    Returns the new run's id, or ``None`` if any skip condition fired
    (schedule not found, paused, payload missing ``repo``, no
    WorkflowVersion seeded, or the deterministic stuck-task sweep ran —
    no run is materialized).
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

    # Deterministic-detector intercept (ADR-0047): the stuck-task sweep is
    # a query, not a role. Drive it directly off the scheduled tick.
    from treadmill_api.coordination.stuck_task_sweep import (
        STUCK_TASK_SWEEP_WORKFLOW_ID,
        run_stuck_task_sweep,
    )
    if schedule.workflow_id == STUCK_TASK_SWEEP_WORKFLOW_ID:
        await run_stuck_task_sweep(session, dispatcher)
        return None

    # Same idiom for the ADR-0062 escalation-close sweep — the five
    # close triggers are deterministic queries over the event stream;
    # no role-step needed.
    from treadmill_api.coordination.escalation_close_sweep import (
        ESCALATION_CLOSE_SWEEP_WORKFLOW_ID,
        run_escalation_close_sweep,
    )
    if schedule.workflow_id == ESCALATION_CLOSE_SWEEP_WORKFLOW_ID:
        await run_escalation_close_sweep(session, dispatcher)
        return None

    # Same idiom for the terminal-gate orphan sweep (ADR-0047, ADR-0038,
    # ADR-0042) — detects architect-accepted PRs not yet merged; pure
    # query, no role-step needed.
    from treadmill_api.coordination.terminal_gate_sweep import (
        TERMINAL_GATE_SWEEP_WORKFLOW_ID,
        run_terminal_gate_sweep,
    )
    if schedule.workflow_id == TERMINAL_GATE_SWEEP_WORKFLOW_ID:
        await run_terminal_gate_sweep(session, dispatcher)
        return None

    # Same idiom for the step-starvation sweep (ADR-0075) — detects steps
    # queued for dispatch (step.ready) that never reach execution
    # (step.started); pure query, no role-step needed.
    from treadmill_api.coordination.step_starvation_sweep import (
        STEP_STARVATION_SWEEP_WORKFLOW_ID,
        run_step_starvation_sweep,
    )
    if schedule.workflow_id == STEP_STARVATION_SWEEP_WORKFLOW_ID:
        await run_step_starvation_sweep(session, dispatcher)
        return None

    # Same idiom for the fleet-wedge sweep (ADR-0075 §3) — detects
    # families wedged at worker_count=0 per the autoscaler's
    # system_status heartbeats; emits ``system.fleet_wedged``. v1 ships
    # only the zero-workers sub-signal.
    from treadmill_api.coordination.fleet_wedge_sweep import (
        FLEET_WEDGE_SWEEP_WORKFLOW_ID,
        run_fleet_wedge_sweep,
    )
    if schedule.workflow_id == FLEET_WEDGE_SWEEP_WORKFLOW_ID:
        await run_fleet_wedge_sweep(session, dispatcher)
        return None

    # Same idiom for the unreferenced-close-report sweep — sweeps past 7 days
    # of escalation closes with null/empty expected_followup, groups by repo,
    # and emits one report per repo for NotificationFanout consumption.
    from treadmill_api.coordination.unreferenced_close_report import (
        UNREFERENCED_CLOSE_REPORT_WORKFLOW_ID,
        run_unreferenced_close_report_sweep,
    )
    if schedule.workflow_id == UNREFERENCED_CLOSE_REPORT_WORKFLOW_ID:
        await run_unreferenced_close_report_sweep(session, dispatcher)
        return None

    repo = typed.rendered_payload.get("repo")
    if not repo:
        # ADR-0057 + ADR-0055 sibling: schedules without a payload.repo can't
        # name a Task (Task.repo is NOT NULL) and can't populate step.ready.
        # The synthetic-task fix surfaces this as a loud skip instead of
        # the previous silent ``repo=""`` propagation.
        logger.warning(
            "scheduled-tick: schedule %s rendered_payload missing 'repo'; "
            "skipping dispatch for workflow %s",
            typed.schedule_id, schedule.workflow_id,
        )
        return None

    # Coalesce prior pending ticks for this schedule before dispatching a
    # fresh one. The 2026-06-03 6-tick wf-ui-triage backlog showed that a
    # role whose start latency exceeds the cron interval pile up: each
    # tick produced a registered task whose step never reached
    # ``started_at``, and the dashboard's overview surface drove 6 visible
    # rows from one logical bot. Coalesce keys off the workflow's latest
    # ``WorkflowVersion`` because there is no schedule_id column on
    # tasks; in steady-state a schedule binds one workflow and resolves
    # to one WV per tick, so this approximates "this schedule's prior
    # pending ticks." The helper runs AFTER the two short-circuits above
    # (sweeps don't synthesize Tasks) and BEFORE
    # ``_dispatch_via_synthetic_task`` (so the newer tick survives, the
    # older pendings are marked cancelled).
    wv_for_coalesce = (
        await session.execute(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == schedule.workflow_id)
            .order_by(WorkflowVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if wv_for_coalesce is not None:
        await _coalesce_pending_ticks_for_schedule(
            session,
            dispatcher,
            schedule_id=schedule.id,
            workflow_version_id=wv_for_coalesce.id,
        )

    # ``ScheduledTick`` carries no fire_at field (ADR-0035 v0); the
    # Task's server-default ``created_at`` is the dispatch timestamp.
    return await _dispatch_via_synthetic_task(
        session,
        dispatcher,
        workflow_id=schedule.workflow_id,
        repo=repo,
        trigger=f"schedule:{typed.schedule_id}",
        created_by="scheduler",
        title=f"schedule:{schedule.workflow_id}",
    )


async def _coalesce_pending_ticks_for_schedule(
    session: AsyncSession,
    dispatcher: Any,
    *,
    schedule_id: uuid.UUID,
    workflow_version_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Cancel prior pending ticks for the same schedule before dispatching
    a fresh one. Returns the list of cancelled task ids (empty when
    nothing to coalesce).

    "Pending" means the task has been registered but no step has begun
    execution — i.e., no row in ``workflow_run_steps`` with a non-null
    ``started_at``. A tick whose first step is mid-flight is left alone
    (parallel runs are an explicit non-goal; we only collapse the queue
    of unstarted dupes).

    Match criteria (the (workflow_id, schedule_id) intent — schedule_id
    is not stored on the task row, so the latest WV the schedule
    dispatches under is the proxy):

      * ``plan_id = SYSTEM_PLAN_ID``
      * ``created_by = 'scheduler'``
      * ``workflow_version_id = :wv_id``
      * NO workflow_run_step row exists with non-null ``started_at``
        for this task (still pending)
      * NO terminal task lifecycle event exists for this task
        (``cancelled`` / ``superseded`` / ``escalated_to_operator``) —
        a prior pending that already got terminalized by some other
        path is left alone.

    For each match the helper emits ``task.cancelled`` via the existing
    ``persist_and_publish`` seam with ``reason='superseded_by_newer_tick'``,
    ``schedule_id=<schedule_id>``, ``cancelled_by='scheduler-coalesce'``.
    The caller commits the session — same transactional contract as
    ``_dispatch_via_synthetic_task``.
    """
    stmt = text(
        """
        SELECT t.id
        FROM tasks t
        WHERE t.plan_id = :plan_id
          AND t.created_by = 'scheduler'
          AND t.workflow_version_id = :wv_id
          AND NOT EXISTS (
            SELECT 1
            FROM workflow_runs r
            JOIN workflow_run_steps s ON s.run_id = r.id
            WHERE r.task_id = t.id
              AND s.started_at IS NOT NULL
          )
          AND NOT EXISTS (
            SELECT 1
            FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'task'
              AND e.action IN (
                'cancelled', 'superseded', 'escalated_to_operator'
              )
          )
        """
    )
    result = await session.execute(
        stmt,
        {"plan_id": SYSTEM_PLAN_ID, "wv_id": workflow_version_id},
    )
    task_ids: list[uuid.UUID] = list(result.scalars())

    for task_id in task_ids:
        await dispatcher.persist_and_publish(
            session,
            entity_type="task",
            action="cancelled",
            payload=TaskCancelled(
                reason="superseded_by_newer_tick",
                schedule_id=schedule_id,
                cancelled_by="scheduler-coalesce",
            ),
            plan_id=SYSTEM_PLAN_ID,
            task_id=task_id,
        )

    if task_ids:
        logger.info(
            "scheduled-tick coalesce: cancelled %d prior pending tick(s) "
            "for schedule %s (wv=%s)",
            len(task_ids), schedule_id, workflow_version_id,
        )

    return task_ids


async def _dispatch_via_synthetic_task(
    session: AsyncSession,
    dispatcher: Any,
    *,
    workflow_id: str,
    repo: str,
    trigger: str,
    created_by: str,
    title: str,
) -> uuid.UUID | None:
    """ADR-0057: create a synthetic ``Task`` and dispatch via the normal
    task-bound path so workers see a normal task body.

    Mirrors ``create_task`` in ``routers/tasks.py`` — resolve the latest
    ``WorkflowVersion``, insert a ``Task`` tied to ``SYSTEM_PLAN_ID``,
    emit ``task.registered``, then call ``dispatcher.dispatch_task``.

    ``trigger`` is recorded as a tag on the ``Task.title`` and on the
    surrounding log line; the underlying ``WorkflowRun.trigger`` will be
    ``"registered"`` (the dispatch_task default). Schedulers and operator
    surfaces can still distinguish themselves via ``created_by``.

    Returns the run's id, or ``None`` if the workflow has no version
    (un-seeded install) or no steps (degenerate workflow). The caller
    must commit the session.
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
            "synthetic-task dispatch (%s): no WorkflowVersion for %s; "
            "skipping (run starters seed?)",
            trigger, workflow_id,
        )
        return None

    task = Task(
        plan_id=SYSTEM_PLAN_ID,
        repo=repo,
        title=title,
        description=None,
        workflow_version_id=wv.id,
        created_by=created_by,
    )
    session.add(task)
    await session.flush()

    # Mirror create_task: emit TaskRegistered before dispatch so the audit
    # log carries the registration before the run is materialized.
    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="registered",
        payload=TaskRegistered(
            repo=task.repo,
            title=task.title,
            workflow_version_id=wv.id,
            plan_id=SYSTEM_PLAN_ID,
        ),
        plan_id=SYSTEM_PLAN_ID,
        task_id=task.id,
    )

    try:
        run_id = await dispatcher.dispatch_task(session, task)
    except Exception:
        logger.exception(
            "synthetic-task dispatch (%s): dispatch_task raised for "
            "workflow %s, task %s",
            trigger, workflow_id, task.id,
        )
        raise

    logger.info(
        "synthetic-task dispatch (%s): workflow %s → task %s, run %s",
        trigger, workflow_id, task.id, run_id,
    )
    return run_id


async def _create_and_publish_run_without_task(
    session: AsyncSession,
    dispatcher: Any,
    *,
    workflow_id: str,
    trigger: str,
    repo: str,
) -> uuid.UUID | None:
    """**DEPRECATED — do not call.** Use ``_dispatch_via_synthetic_task``.

    Per ADR-0057, this path is the 4th silent-failure pattern in the
    scheduler primitive: it sends ``task_id=null`` / ``plan_id=null`` to
    workers; workers parse the body without KeyError but die silently
    inside ``_handle_step`` (downstream code assumes non-null task), and
    SQS visibility-timeout looping never publishes a ``step.failed``.

    Kept in tree for the 4 historical orphan ``workflow_runs`` rows the
    pre-fix scheduler created, and to keep the unit tests around the
    legacy shape running while we wind it down. New callers are not
    permitted; both production surfaces (``handle_scheduled_tick`` +
    ``routers/workflow_triggers``) go through ``_dispatch_via_synthetic_task``.

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

# Workflow-set gate removed 2026-05-18 (round-down of the architectural
# fragility surfaced by PR #154). Previously, the predicate fired only
# on step.completed for an explicit allowlist of workflows
# (``wf-validate`` / ``wf-review`` / later ``wf-architecture-resolve``).
# Every new override-emitting workflow required extending the set or
# the PR sat at ``derived_mergeability=mergeable`` indefinitely. The
# predicate's own mergeability-state checks already short-circuit on
# non-relevant signals, so the workflow gate added only architectural
# coupling without protective value. Removing the gate makes the
# predicate self-correcting: it fires on every step.completed, the
# task_mergeability VIEW evaluates fresh, and the deadline is set iff
# the current mergeability state warrants it. Cost: one VIEW read per
# step.completed (predicate fast-bails when mergeability isn't ready).
# Benefit: future override channels — `ci.override`, hypothetical
# `conflict.override`, etc. — fire naturally without trigger-set
# bookkeeping. Closes the architectural gap captured in
# docs/learnings/2026-05-18-auto-merge-misses-architect-override-
# completion.md §"long-term design proposal" without taking on the
# full task_mergeability.changed-event projection (still a future
# refinement).


async def _repo_auto_merge_blocked(session: AsyncSession, repo: str) -> bool:
    """True iff the repo's onboarding config blocks all auto-merge
    (ADR-0050 d.5). Fail-OPEN: missing config or any error -> False,
    preserving pre-ADR-0050 behavior for repos without a config row."""
    if not repo:
        return False
    try:
        config = await OnboardingStore().get_repo_config(session, repo)
    except Exception:
        logger.warning(
            "auto-merge: repo_config lookup failed for %s; fail-open",
            repo,
            exc_info=True,
        )
        return False
    return bool(config and config.auto_merge_blocked)


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
      * ``redis_client`` not wired (auto-merge poll loop inoperable).
      * Step has no owning task (orphan or deleted).
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

    return await _arm_auto_merge_for_task(session, redis_client, row.task_id)


async def maybe_auto_merge_on_github_event(
    session: AsyncSession,
    redis_client: Any,
    *,
    repo: str,
    pr_number: int | None,
) -> bool:
    """Set/push the auto-merge cooling-off deadline when a ``github.*`` event
    that can flip mergeability lands — closing the arming-coverage gap behind
    accept-as-is / late-CI orphans.

    ``maybe_auto_merge_on_mergeable`` only fires on ``step.completed``. A task
    can cross into ``derived_mergeability='mergeable'`` because of a github
    event that is **not** a step completion — CI finishing green
    (``check_run_completed``), a clean re-push (``pr_synchronize``), a conflict
    clearing, or a late ``pr_opened``. Without this entrypoint those
    transitions never re-arm and a green, approved PR strands (the
    accept-as-is / #133 / #185 orphan class; see
    ``docs/plans/2026-06-05-accept-as-is-open-pr-not-terminal.md`` M2).

    Resolves ``task_id`` from ``task_prs (repo, pr_number)`` — every
    mergeability-affecting github verb carries both — then delegates to the
    shared arming body. It is **strictly safe**: the body only sets the
    deadline when the mergeability VIEW already reads ``mergeable`` (validate
    pass + review approved + CI ok + no conflict), and ``fire_elapsed_auto_merges``
    re-verifies before the merge PUT, so this can never merge anything that
    isn't already fully mergeable.

    Returns ``True`` if the deadline was set/pushed, ``False`` otherwise.
    """
    if redis_client is None:
        return False
    if not repo or pr_number is None:
        return False

    result = await session.execute(
        select(TaskPR.task_id).where(
            TaskPR.repo == repo,
            TaskPR.pr_number == pr_number,
        )
    )
    row = result.first()
    if row is None:
        logger.debug(
            "auto-merge: no task_pr for %s#%s; skipping github-event arm",
            repo, pr_number,
        )
        return False

    return await _arm_auto_merge_for_task(session, redis_client, row.task_id)


async def _arm_auto_merge_for_task(
    session: AsyncSession,
    redis_client: Any,
    task_id: Any,
) -> bool:
    """Shared arming body. Given a resolved ``task_id``, check the
    ``task_mergeability`` VIEW + plan / repo opt-outs and set (or push) the
    30-second cooling-off deadline. Reached from both the ``step.completed``
    path (``maybe_auto_merge_on_mergeable``) and the github-event path
    (``maybe_auto_merge_on_github_event``); the skip-condition semantics are
    identical regardless of which signal triggered the re-evaluation."""
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

    if await _repo_auto_merge_blocked(session, merge_row.repo):
        logger.info(
            "auto-merge: repo %s auto_merge_blocked (ADR-0050); "
            "skipping task %s", merge_row.repo, task_id,
        )
        return False

    # Not currently mergeable.
    #
    # Promoted from debug → info on 2026-05-17 (PR for task #135): silent
    # bailouts hid a transaction-flush race for hours on PRs #132/#133 and
    # again on the worker-PR cohort #136/#137/#138. The skip path is a
    # legitimate read of the VIEW (pre-merge state is normal), but a
    # *stuck* PR with this log line at info-level is now grep-able. If
    # this becomes too noisy at steady state, demote with a sampling
    # filter rather than silencing entirely.
    if merge_row.derived_mergeability != "mergeable":
        logger.info(
            "auto-merge: task %s mergeability=%r; skipping",
            task_id, merge_row.derived_mergeability,
        )
        return False

    # ADR-0031 Q31.b: only 'pass' auto-merges; 'uncertain' routes to wf-feedback.
    if merge_row.validate_decision != "pass":
        logger.info(
            "auto-merge: validate_decision=%r for task %s; skipping",
            merge_row.validate_decision, task_id,
        )
        return False

    # Pending human review (review_decision must be 'approved').
    if merge_row.review_decision != "approved":
        logger.info(
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
            SELECT tm.derived_mergeability, tm.repo, p.auto_merge
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
    if await _repo_auto_merge_blocked(session, row.repo):
        return False
    return row.derived_mergeability == "mergeable"


# ── ADR-0083: architect emit-failure relay drop ──────────────────────────────


def maybe_drop_relay_on_architect_emit_failure(
    payload: ArchitectEmitFailure,
    task_id: str,
    *,
    relay_base: Path | None = None,
) -> Path | None:
    """Write a cc-relay notification file when the architect fails to emit a
    structured verdict (ADR-0083).

    Drops a markdown file into ``~/.cc-channels/<created_by>/relay/`` named
    ``architect-emit-failure-<failing_run_id>.md`` so the dispatching
    orchestrator session's treadmill-events server picks it up via inotify.

    The filename is keyed solely on ``failing_run_id`` for idempotency: SQS
    redelivery of the same event overwrites the same file rather than
    producing a relay-spam cascade.

    ``relay_base`` overrides the base directory for tests. In production
    it falls back to the ``TREADMILL_CC_CHANNELS_DIR`` env var, then
    ``Path.home() / ".cc-channels"``.

    **Dev-local deployment note:** the API service runs in Docker with no
    ``~/.cc-channels/`` volume mount, so this write is a no-op in the
    container environment. A volume mount (e.g.
    ``~/.cc-channels:/root/.cc-channels``) or the ``TREADMILL_CC_CHANNELS_DIR``
    env var pointing at a writable path is required. Production-split
    deployments need a remote-drop mechanism (SQS to orchestrator host);
    out of v1 scope — flagged in AGENT.md.
    """
    if relay_base is None:
        env_base = os.environ.get("TREADMILL_CC_CHANNELS_DIR")
        relay_base = Path(env_base) if env_base else Path.home() / ".cc-channels"

    relay_dir = relay_base / payload.created_by / "relay"
    try:
        relay_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning(
            "architect-emit-failure relay: cannot create relay dir %s; skipping drop",
            relay_dir,
        )
        return None

    filename = f"architect-emit-failure-{payload.failing_run_id}.md"
    relay_path = relay_dir / filename

    body = (
        f"# Architect emit-failure\n\n"
        f"**Task:** {task_id}  \n"
        f"**Run:** {payload.failing_run_id}  \n"
        f"**Reason:** `{payload.parse_failure_reason}`  \n\n"
        f"## Model output excerpt\n\n"
        f"```\n{payload.model_output_excerpt[:4096]}\n```\n\n"
        f"---\n\n"
        f"The architect's `--json-schema` call did not produce a structured verdict.\n"
        f"Check the run in the Treadmill dashboard or query the DB:\n\n"
        f"```sql\nSELECT * FROM workflow_runs WHERE id = '{payload.failing_run_id}';\n```\n"
    )
    relay_path.write_text(body)
    logger.info(
        "architect-emit-failure relay: dropped %s for label %s run %s reason %s",
        relay_path,
        payload.created_by,
        payload.failing_run_id,
        payload.parse_failure_reason,
    )
    return relay_path
