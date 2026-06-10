"""Tasks router. Read-mostly per the Phase 2 plan; the primary creation
path is via Plans (which spawns Tasks from a parsed plan-doc). This router
exposes ``GET /tasks/{id}`` and ``GET /tasks`` for inspection, and a
small ``POST /tasks`` for direct submissions when the parent Plan exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.coordination.coordinator_overlay import (
    CapOverlayDecision,
    coordinator_overlay_decision,
)
from treadmill_api.coordination.triggers import (
    _create_and_publish_run,
    _is_capped,
    infer_retry_workflow,
)
from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher
from treadmill_api.events.task import ArchitectEmitFailure, TaskRegistered, TaskRetry, TaskWorkerHintRequested
from treadmill_api.models import (
    Plan,
    Task,
    Workflow,
    WorkflowDispatchDedup,
    WorkflowRun,
    WorkflowVersion,
)
from treadmill_api.events.task import OperatorHintSet


router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class TaskCreateRequest(BaseModel):
    plan_id: uuid.UUID
    title: str = Field(..., min_length=1, max_length=512)
    description: str | None = None
    workflow: str = Field(..., min_length=1, max_length=64)
    """Workflow slug; the latest version is pinned at submission time."""

    created_by: str | None = None


class TaskResponse(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    repo: str
    title: str
    description: str | None
    workflow_version_id: uuid.UUID
    created_by: str | None
    created_at: datetime
    parent_task_id: uuid.UUID | None = None
    """Per ADR-0048: when the architect verdicts ``supersede``, a child
    task is created with the rewritten description and ``parent_task_id``
    pointing back to the original. ``None`` for tasks that did not
    originate from a supersede."""
    operator_note: str | None = None
    """Per ADR-0081: operator-injected hint for the worker. Nullable and
    defaults to None."""
    derived_status: str | None = None
    mergeability: str | None = None
    """The ``derived_mergeability`` from the ``task_mergeability`` VIEW
    (ADR-0013). ``None`` when the task has no PR yet — the VIEW joins
    on ``task_prs`` so there is no row to read. See
    ``GET /tasks/{id}/mergeability`` for the full row."""


class MergeabilityResponse(BaseModel):
    """Focused projection of ``task_mergeability`` (ADR-0013).

    Reserved for a future auto-merge orchestrator's polling per the
    ADR's "auto-merge upgrade path". Single-purpose endpoint — does not
    widen the task contract.
    """

    task_id: uuid.UUID
    repo: str | None
    pr_number: int | None
    head_sha: str | None
    review_decision: str | None
    validate_decision: str | None
    ci_conclusion: str | None
    pr_conflicting: bool | None
    derived_mergeability: str
    """Never null — defaults to ``'pending'`` when no PR row exists."""


class TaskRetryRequest(BaseModel):
    workflow_id: str | None = None
    """Workflow slug to re-dispatch. If omitted, inferred from the most-recent
    non-terminal run per ADR-0046."""
    reason: str = Field(..., min_length=1, max_length=500)
    force_bypass_cap: bool = False


class TaskRetryResponse(BaseModel):
    workflow_run_id: uuid.UUID


def _row_to_response(row) -> TaskResponse:
    return TaskResponse(
        id=row.id, plan_id=row.plan_id, repo=row.repo,
        title=row.title, description=row.description,
        workflow_version_id=row.workflow_version_id,
        created_by=row.created_by, created_at=row.created_at,
        parent_task_id=row.parent_task_id,
        operator_note=row.operator_note,
        derived_status=row.derived_status,
        mergeability=row.derived_mergeability,
    )


_TASK_WITH_STATUS_SQL = """
    SELECT t.id, t.plan_id, t.repo, t.title, t.description,
           t.workflow_version_id, t.created_by, t.created_at,
           t.parent_task_id, t.operator_note,
           ts.derived_status,
           tm.derived_mergeability
    FROM tasks t
    LEFT JOIN task_status ts ON ts.id = t.id
    LEFT JOIN task_mergeability tm ON tm.task_id = t.id
"""


async def _resolve_workflow_version(session: AsyncSession, slug: str) -> uuid.UUID:
    workflow = await session.get(Workflow, slug)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workflow {slug!r} not registered",
        )
    result = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == slug)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workflow {slug!r} has no versions yet",
        )
    return version.id


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> TaskResponse:
    plan = await session.get(Plan, body.plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"plan {body.plan_id} not found",
        )
    wv_id = await _resolve_workflow_version(session, body.workflow)
    task = Task(
        plan_id=plan.id, repo=plan.repo,
        title=body.title, description=body.description,
        workflow_version_id=wv_id, created_by=body.created_by,
    )
    session.add(task)
    await session.flush()
    # A.6 — emit TaskRegistered; coordinator picks up via task.registered WS event.
    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="registered",
        payload=TaskRegistered(
            repo=task.repo,
            title=task.title,
            workflow_version_id=wv_id,
            plan_id=plan.id,
        ),
        plan_id=plan.id,
        task_id=task.id,
    )
    await session.commit()
    await session.refresh(task)

    # Re-fetch with derived_status from the VIEW.
    row = (
        await session.execute(
            text(_TASK_WITH_STATUS_SQL + " WHERE t.id = :id"),
            {"id": task.id},
        )
    ).one()
    return _row_to_response(row)


_TASK_NEEDS_OPERATOR_SQL = """
    SELECT DISTINCT t.id, t.plan_id, t.repo, t.title, t.description,
           t.workflow_version_id, t.created_by, t.created_at,
           t.parent_task_id, t.operator_note,
           ts.derived_status,
           tm.derived_mergeability
    FROM tasks t
    LEFT JOIN task_status ts ON ts.id = t.id
    LEFT JOIN task_mergeability tm ON tm.task_id = t.id
    INNER JOIN events e ON e.task_id = t.id
        AND e.entity_type = 'task' AND e.action = 'escalated_to_operator'
"""


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    session: Annotated[AsyncSession, Depends(get_session)],
    repo: Annotated[str | None, Query()] = None,
    plan_id: Annotated[uuid.UUID | None, Query()] = None,
    derived_status: Annotated[str | None, Query()] = None,
) -> list[TaskResponse]:
    """List tasks with optional filters by repo, plan_id, or derived_status.

    Special value ``derived_status=needs_operator`` returns tasks that have
    a ``task.escalated_to_operator`` event — i.e. tasks where the architect
    cap fired and operator intervention is required (ADR-0048 §3).
    """
    if derived_status == "needs_operator":
        sql = _TASK_NEEDS_OPERATOR_SQL + " WHERE 1=1"
        params: dict[str, object] = {}
        if repo is not None:
            sql += " AND t.repo = :repo"
            params["repo"] = repo
        if plan_id is not None:
            sql += " AND t.plan_id = :plan_id"
            params["plan_id"] = plan_id
        sql += " ORDER BY t.created_at DESC LIMIT 500"
        result = await session.execute(text(sql), params)
        return [_row_to_response(row) for row in result]

    sql = _TASK_WITH_STATUS_SQL + " WHERE 1=1"
    params = {}
    if repo is not None:
        sql += " AND t.repo = :repo"
        params["repo"] = repo
    if plan_id is not None:
        sql += " AND t.plan_id = :plan_id"
        params["plan_id"] = plan_id
    if derived_status is not None:
        sql += " AND ts.derived_status = :ds"
        params["ds"] = derived_status
    sql += " ORDER BY t.created_at DESC LIMIT 500"
    result = await session.execute(text(sql), params)
    return [_row_to_response(row) for row in result]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskResponse:
    row = (
        await session.execute(
            text(_TASK_WITH_STATUS_SQL + " WHERE t.id = :id"),
            {"id": task_id},
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return _row_to_response(row)


_MERGEABILITY_SQL = """
    SELECT t.id AS task_id,
           tm.repo, tm.pr_number, tm.head_sha,
           tm.review_decision, tm.validate_decision,
           tm.ci_conclusion, tm.pr_conflicting,
           tm.derived_mergeability
    FROM tasks t
    LEFT JOIN task_mergeability tm ON tm.task_id = t.id
    WHERE t.id = :id
"""


@router.get("/{task_id}/mergeability", response_model=MergeabilityResponse)
async def get_task_mergeability(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MergeabilityResponse:
    """Return the ``task_mergeability`` row for a task per ADR-0013.

    The VIEW joins ``task_prs``, so a task with no PR has no row in
    ``task_mergeability``. We surface that as ``derived_mergeability =
    'pending'`` with every other field NULL — the auto-merge orchestrator
    treats it the same as "head sha unknown".
    """

    row = (
        await session.execute(
            text(_MERGEABILITY_SQL),
            {"id": task_id},
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )
    return MergeabilityResponse(
        task_id=row.task_id,
        repo=row.repo,
        pr_number=row.pr_number,
        head_sha=row.head_sha,
        review_decision=row.review_decision,
        validate_decision=row.validate_decision,
        ci_conclusion=row.ci_conclusion,
        pr_conflicting=row.pr_conflicting,
        derived_mergeability=row.derived_mergeability or "pending",
    )


@router.post(
    "/{task_id}/retry",
    response_model=TaskRetryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def retry_task(
    task_id: uuid.UUID,
    body: TaskRetryRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> TaskRetryResponse:
    """Operator-driven retry of a stuck task's most-recent workflow (ADR-0046).

    Clears the matching dedup row(s), emits a task.retry audit event, and
    dispatches a fresh workflow run. Respects the per-workflow attempt cap
    unless force_bypass_cap is set.
    """
    # 1. 404 if task not found.
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )

    # 2. Resolve workflow_id (explicit or inferred).
    workflow_id = body.workflow_id
    if workflow_id is None:
        workflow_id = await infer_retry_workflow(session, task_id)
    if workflow_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no retryable workflow found; pass workflow_id explicitly",
        )

    # Capture most-recent run id for the audit event before any mutations.
    prev_result = await session.execute(
        select(WorkflowRun.id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
        .where(
            WorkflowRun.task_id == task_id,
            WorkflowVersion.workflow_id == workflow_id,
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    )
    previous_run_id = prev_result.scalar_one_or_none()

    # 3a. ADR-0084 Task 2B — coordinator overlay runs before the cap.
    # When the plan has a blocked_operator task AND the coordinator is
    # alive, manual retry is refused with a different 409 message so
    # the operator sees why. ``force_bypass_cap`` overrides both gates
    # (single bypass flag covers operator-knows-better escapes).
    if not body.force_bypass_cap:
        overlay = await coordinator_overlay_decision(session, task_id)
        if overlay is CapOverlayDecision.BLOCK_BY_COORDINATOR:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "plan is in coordinator-blocked state "
                    "(task_board status = blocked_operator); coordinator "
                    "must release before retry, or pass "
                    "force_bypass_cap=true to override"
                ),
            )

    # 3b. Check cap; 409 if at cap and force_bypass_cap not set.
    if await _is_capped(session, task_id, workflow_id) and not body.force_bypass_cap:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cap reached; pass force_bypass_cap=true",
        )

    # 4. Clear matching workflow_dispatch_dedup rows for every run of the
    # target workflow on this task (the rows that would block re-dispatch).
    await session.execute(
        delete(WorkflowDispatchDedup).where(
            WorkflowDispatchDedup.workflow_run_id.in_(
                select(WorkflowRun.id)
                .join(
                    WorkflowVersion,
                    WorkflowVersion.id == WorkflowRun.workflow_version_id,
                )
                .where(
                    WorkflowRun.task_id == task_id,
                    WorkflowVersion.workflow_id == workflow_id,
                )
            )
        )
    )

    # 5. Emit task.retry audit event.
    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="retry",
        payload=TaskRetry(
            workflow_id=workflow_id,
            reason=body.reason,
            by_operator="operator",
            bypassed_cap=body.force_bypass_cap,
            previous_run_id=str(previous_run_id) if previous_run_id else None,
        ),
        plan_id=task.plan_id,
        task_id=task_id,
    )

    # 6. Create new workflow run + publish step.ready.
    # Uses _create_and_publish_run (same path as the trigger evaluator) rather
    # than dispatch_task — the latter has an idempotency guard that returns the
    # existing run when step.ready already exists for the task.
    run_id = await _create_and_publish_run(
        session,
        dispatcher,
        task=task,
        workflow_id=workflow_id,
        trigger="operator:task-retry",
    )
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workflow {workflow_id!r} has no version or no steps",
        )

    await session.commit()

    # 7. Return 201 with the new run id.
    return TaskRetryResponse(workflow_run_id=run_id)


class OperatorNoteRequest(BaseModel):
    note: str | None = None
    """The operator hint text, or null to clear the note."""


@router.post("/{task_id}/operator_note", status_code=status.HTTP_200_OK)
async def set_operator_note(
    task_id: uuid.UUID,
    body: OperatorNoteRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> TaskResponse:
    """Set or clear the operator_note on a task (ADR-0081 §1).

    The operator uses this endpoint to inject context hints for the worker
    before the next step executes. Workers read this via the per-step context
    fetch and inject it into the system prompt when non-null and the repo's
    worker_hints_enabled is true.

    Request: ``{note: str | null}``
      - Provide a string to set the note.
      - Pass null to clear it.

    Response: The updated task record.

    Events: Emits ``task.operator_hint_set`` with the note excerpt and
    operator label for the audit trail.
    """
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )

    # Update the note
    task.operator_note = body.note

    # Prepare event payload
    note_for_event = body.note
    if note_for_event is None:
        note_excerpt = "(cleared)"
    else:
        note_excerpt = note_for_event[:500]

    # Emit the audit event
    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="operator_hint_set",
        payload=OperatorHintSet(
            note_excerpt=note_excerpt,
            set_by="operator",  # TODO: extract from auth context when available
        ),
        plan_id=task.plan_id,
        task_id=task_id,
    )

    await session.commit()
    await session.refresh(task)

    # Return the updated task
    row = (
        await session.execute(
            text(_TASK_WITH_STATUS_SQL + " WHERE t.id = :id"),
            {"id": task.id},
        )
    ).one()
    return _row_to_response(row)


class WorkerHintRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=100)
    """Short slug naming the class of help wanted."""
    context_excerpt: str = Field(..., min_length=1, max_length=500)
    """Brief excerpt of context (first 500 chars)."""
    worker_step_id: str = Field(..., min_length=1)
    """The step ID of the worker step making the request."""


@router.post("/{task_id}/worker_hint_request", status_code=status.HTTP_200_OK)
async def worker_hint_request(
    task_id: uuid.UUID,
    body: WorkerHintRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> None:
    """Worker requests operator context (ADR-0081 §2).

    The worker invokes this endpoint when stuck and needs operator context.
    The endpoint emits a ``task.worker_hint_requested`` event that surfaces
    the request to the operator session via cc-channels.

    Request: ``{reason: str, context_excerpt: str, worker_step_id: str}``
      - reason: Short slug (e.g. 'tests_need_scope')
      - context_excerpt: Brief description of the problem (max 500 chars)
      - worker_step_id: The step ID making the request

    Response: 200 OK (no body)

    Events: Emits ``task.worker_hint_requested`` with the reason, context,
    and worker_step_id for the audit trail.
    """
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )

    # Emit the audit event
    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="worker_hint_requested",
        payload=TaskWorkerHintRequested(
            reason=body.reason,
            context_excerpt=body.context_excerpt,
            worker_step_id=body.worker_step_id,
        ),
        plan_id=task.plan_id,
        task_id=task_id,
    )

    await session.commit()


class ArchitectEmitFailureRequest(BaseModel):
    parse_failure_reason: Literal[
        "no-structured-output",
        "supersede-missing-rewrite",
        "gate-broken-missing-excerpt",
        "invalid-verdict-literal",
    ]
    model_output_excerpt: str = Field(..., max_length=4096)
    created_by: str = Field(..., min_length=1)
    failing_run_id: str = Field(..., min_length=1)


@router.post("/{task_id}/architect_emit_failure", status_code=status.HTTP_200_OK)
async def architect_emit_failure(
    task_id: uuid.UUID,
    body: ArchitectEmitFailureRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> None:
    """Worker reports that the architect role failed to emit a parseable verdict (ADR-0083).

    The worker calls this when ``--json-schema`` structured output is absent or
    fails post-emit validation. The endpoint persists the event and the
    coordination trigger drops a relay file into the dispatching orchestrator's
    cc-channels inbox.

    Request: ``{parse_failure_reason, model_output_excerpt, created_by, failing_run_id}``

    Response: 200 OK (no body)

    Events: Emits ``task.architect_emit_failure`` for the audit trail and relay trigger.
    """
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )

    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="architect_emit_failure",
        payload=ArchitectEmitFailure(
            parse_failure_reason=body.parse_failure_reason,
            model_output_excerpt=body.model_output_excerpt,
            created_by=body.created_by,
            failing_run_id=body.failing_run_id,
        ),
        plan_id=task.plan_id,
        task_id=task_id,
    )

    await session.commit()
