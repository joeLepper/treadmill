"""Cross-step dispatch — when step N of a run completes, fire step N+1.

Per ADR-0015 §"Cross-step dispatch", the coordination consumer takes
over after step 1 of a multi-step run completes. The dispatcher
(``dispatch.py``) handles single-step firing for the first step of a
run (via ``dispatch_task``); this module handles every subsequent step.

The contract:

* When ``step.completed`` arrives for a run whose workflow has a next
  pending step, this module materializes the SQS claim + publishes the
  ``step.ready`` Event row.
* When ``step.failed`` arrives for a run whose workflow has a next
  pending step, the next step still runs — per ADR-0015 §"No
  cancellation; no step skipping" — but the action role sees the prior
  step's ``decision='blocked'`` and emits its own no-op.
* Publish failures route through the existing ``dispatch_publish_failed``
  marker + replay loop (Phase-3 closure A.8 + A.10).

The "find the next pending step" pattern is cribbed from bunkhouse
``events/consumer.py:_on_step_completed`` (around line 524). Treadmill's
dispatcher pre-creates every ``workflow_run_steps`` row at run-creation
time with ``status='pending'`` (matching bunkhouse), so the next step
is simply the lowest-``step_index`` row past the completed one that is
still ``pending`` *and* doesn't already have a ``step.ready`` Event row.

Idempotency
-----------

A second call for the same ``(run_id, completed_step_index)`` is a
no-op: the SELECT below filters out any next step that already has a
``step.ready`` event keyed on its ``step_id``. So re-delivery of the
prior step's ``step.completed`` event (which the consumer already
treats as a no-op by the WHERE-clause on the status UPDATE) doesn't
re-dispatch the next step either.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.events.step import StepReady
from treadmill_api.models import (
    Event,
    Task,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)

logger = logging.getLogger("treadmill.coordination.cross_step")


async def _resolve_prior_commit_sha(
    session: AsyncSession, run_id: uuid.UUID, completed_step_index: int,
) -> str | None:
    """Read ``commit_sha`` from the most recently-completed prior step's
    envelope. The mergeability VIEW (ADR-0013) joins on this column so
    the next step's ``step.ready`` Event row must carry it through.

    Returns ``None`` when the prior step has no ``commit_sha`` in its
    output (e.g. pre-commit analyzer step on a workflow that never
    resolves to a HEAD — rare; documented).
    """
    result = await session.execute(
        sa.text(
            "SELECT output->>'commit_sha' AS commit_sha "
            "FROM workflow_run_steps "
            "WHERE run_id = :run_id AND step_index = :idx"
        ),
        {"run_id": run_id, "idx": completed_step_index},
    )
    row = result.first()
    if row is None:
        return None
    return row.commit_sha


async def _find_next_pending_step(
    session: AsyncSession, run_id: uuid.UUID, completed_step_index: int,
) -> WorkflowRunStep | None:
    """Find the next pending step in the run, filtering out steps that
    already have a ``step.ready`` Event row (idempotency).

    Cribbed from bunkhouse ``events/consumer.py:_on_step_completed`` ~L514:
    "Determine next pending step (ordered by step_index)". The
    bunkhouse pattern selects ``status='pending'``; we tighten the WHERE
    with ``step_index > completed_step_index`` so a same-index pending
    row (impossible in practice, defensive) doesn't loop.

    The idempotency guard via ``LEFT JOIN events`` filters out a step
    that already has a ``step.ready`` event keyed on its ``step_id`` —
    re-delivery of the prior step's ``completed`` event won't re-fire
    the next step.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT s.id
            FROM workflow_run_steps s
            LEFT JOIN events e
              ON e.step_id = s.id
              AND e.entity_type = 'step'
              AND e.action = 'ready'
            WHERE s.run_id = :run_id
              AND s.step_index > :completed_idx
              AND s.status = 'pending'
              AND e.id IS NULL
            ORDER BY s.step_index ASC
            LIMIT 1
            """
        ),
        {"run_id": run_id, "completed_idx": completed_step_index},
    )
    row = result.first()
    if row is None:
        return None
    # Re-load via ORM so the caller has a typed WorkflowRunStep with the
    # full field set (step_index, step_name, role_id, run_id).
    return await session.get(WorkflowRunStep, row.id)


async def dispatch_next_step(
    session: AsyncSession,
    dispatcher: Any,
    *,
    run_id: uuid.UUID,
    completed_step_index: int,
) -> uuid.UUID | None:
    """Find the next pending step in the run; publish ``step.ready`` +
    send the SQS work-queue claim.

    Returns the next step's id if one was dispatched, or ``None`` if the
    completed step was the last in the workflow version. Idempotent: a
    second call for the same ``(run_id, completed_step_index)`` is a
    no-op (the WHERE clause filters out steps that already have a
    ``step.ready`` event).
    """
    if dispatcher is None:
        logger.debug(
            "cross_step: dispatcher is None; skipping (run_id=%s)", run_id,
        )
        return None

    next_step = await _find_next_pending_step(
        session, run_id, completed_step_index,
    )
    if next_step is None:
        logger.debug(
            "cross_step: no next pending step for run %s after index %d; "
            "run is complete",
            run_id, completed_step_index,
        )
        return None

    # Resolve the workflow_id (slug) + the task's plan_id + repo via a
    # join on the run's workflow_version and parent task. The Event row
    # needs ``plan_id`` + ``task_id`` for the VIEW + audit, and the
    # ``StepReady`` payload needs ``repo`` + ``workflow_id``. Explicit
    # ``select_from`` because the ORM models carry no relationship
    # mappers (per ADR-0011's pragmatic minimum) so SQLAlchemy can't
    # infer the left side.
    result = await session.execute(
        select(
            WorkflowRun.task_id,
            Task.plan_id,
            Task.repo,
            WorkflowVersion.workflow_id,
        )
        .select_from(WorkflowRun)
        .join(Task, Task.id == WorkflowRun.task_id)
        .join(
            WorkflowVersion,
            WorkflowVersion.id == WorkflowRun.workflow_version_id,
        )
        .where(WorkflowRun.id == run_id)
    )
    row = result.first()
    if row is None:
        logger.warning(
            "cross_step: run %s not found in DB; cannot dispatch next step %s",
            run_id, next_step.id,
        )
        return None
    task_id = row.task_id
    plan_id = row.plan_id
    repo = row.repo
    workflow_slug = row.workflow_id

    commit_sha = await _resolve_prior_commit_sha(
        session, run_id, completed_step_index,
    )

    payload = StepReady(
        role_id=next_step.role_id,
        step_index=next_step.step_index,
        step_name=next_step.step_name,
        repo=repo,
        workflow_id=workflow_slug,
    )

    # Persist the Event row before any external I/O so the source of
    # truth is durable even if the bus publish or queue send fails.
    # Mirrors ``dispatch_task``'s contract for the first-step case so
    # the replay loop heals both paths identically (Phase-3 closure A.10).
    ready_event = await dispatcher._persist_event(
        session,
        entity_type="step",
        action="ready",
        payload=payload,
        plan_id=plan_id,
        task_id=task_id,
        run_id=run_id,
        step_id=next_step.id,
        commit_sha=commit_sha,
    )
    try:
        await dispatcher.publisher.publish(ready_event, payload)
    except Exception as exc:
        logger.exception(
            "cross_step: failed to publish step.ready to events bus; "
            "Event row %s persisted, replay loop will retry",
            ready_event.id,
        )
        await dispatcher._record_publish_failed(
            session,
            original_event_id=ready_event.id,
            target="sns",
            error=exc,
            plan_id=plan_id,
            task_id=task_id,
            run_id=run_id,
            step_id=next_step.id,
        )

    # SQS work-queue claim. Body shape mirrors ``dispatch_task`` plus
    # ``commit_sha`` so the worker has the HEAD anchor in-band without a
    # round-trip to the API. The first-step path does not yet stamp
    # commit_sha (A.3 follow-up); the cross-step path does because the
    # prior step's envelope already settled the SHA.
    if dispatcher.sqs_client is not None and dispatcher.work_queue_url is not None:
        try:
            body: dict[str, Any] = {
                "step_id": str(next_step.id),
                "task_id": str(task_id),
                "plan_id": str(plan_id),
                "run_id": str(run_id),
            }
            if commit_sha is not None:
                body["commit_sha"] = commit_sha
            await asyncio.to_thread(
                dispatcher.sqs_client.send_message,
                QueueUrl=dispatcher.work_queue_url,
                MessageBody=json.dumps(body),
                MessageGroupId=str(run_id),
            )
        except Exception as exc:
            logger.exception(
                "cross_step: failed to send claim to work queue for step %s; "
                "replay loop will retry",
                next_step.id,
            )
            await dispatcher._record_publish_failed(
                session,
                original_event_id=ready_event.id,
                target="sqs",
                error=exc,
                plan_id=plan_id,
                task_id=task_id,
                run_id=run_id,
                step_id=next_step.id,
            )

    logger.info(
        "cross_step: dispatched next step %s (index=%d, role=%s) for run %s",
        next_step.id, next_step.step_index, next_step.role_id, run_id,
    )
    return next_step.id
