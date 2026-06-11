"""Tasks router. Read-mostly per the Phase 2 plan; the primary creation
path is via Plans (which spawns Tasks from a parsed plan-doc). This router
exposes ``GET /tasks/{id}`` and ``GET /tasks`` for inspection, and a
small ``POST /tasks`` for direct submissions when the parent Plan exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher
from treadmill_api.events.github import GithubPrConflict
from treadmill_api.events.task import ArchitectEmitFailure, TaskRegistered, TaskRetry, TaskWorkerHintRequested
from treadmill_api.models import (
    Plan,
    Task,
)
from treadmill_api.events.task import OperatorHintSet


router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

logger = logging.getLogger(__name__)


class TaskCreateRequest(BaseModel):
    plan_id: uuid.UUID
    title: str = Field(..., min_length=1, max_length=512)
    description: str | None = None
    workflow: str | None = None
    """Accepted for wire-compat, ignored post-ADR-0087 Phase 5 — tasks
    no longer pin a workflow version; the coordinator decides execution
    at dispatch time."""

    created_by: str | None = None


class TaskResponse(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    repo: str
    title: str
    description: str | None
    workflow_version_id: uuid.UUID | None = None
    """Always null post-ADR-0087 Phase 5 — wire-compat one deprecation
    window."""
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
    workflow_run_id: uuid.UUID | None = None
    """Always null post-ADR-0087 — runs are gone; the coordinator
    re-dispatches via task_executions. Field kept for wire-compat
    one deprecation window."""


def _row_to_response(row) -> TaskResponse:
    return TaskResponse(
        id=row.id, plan_id=row.plan_id, repo=row.repo,
        title=row.title, description=row.description,
        created_by=row.created_by, created_at=row.created_at,
        parent_task_id=row.parent_task_id,
        operator_note=row.operator_note,
        derived_status=row.derived_status,
        mergeability=row.derived_mergeability,
    )


_TASK_WITH_STATUS_SQL = """
    SELECT t.id, t.plan_id, t.repo, t.title, t.description,
           t.created_by, t.created_at,
           t.parent_task_id, t.operator_note,
           ts.derived_status,
           tm.derived_mergeability
    FROM tasks t
    LEFT JOIN task_status ts ON ts.id = t.id
    LEFT JOIN task_mergeability tm ON tm.task_id = t.id
"""


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
    task = Task(
        plan_id=plan.id, repo=plan.repo,
        title=body.title, description=body.description,
        created_by=body.created_by,
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
           t.created_by, t.created_at,
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


async def _fetch_github_mergeable(
    github_client, repo: str, pr_number: int, head_sha: str,
) -> bool | None:
    """Ask GitHub whether the PR is conflicting. ``True``/``False`` only
    when GitHub has a DEFINITIVE answer for the head the VIEW is looking
    at; ``None`` for still-computing, a stale head (a push raced us), or
    any API failure — the caller never writes an indefinite signal.
    """
    try:
        resp = await github_client.get(f"/repos/{repo}/pulls/{pr_number}")
    except Exception:
        logger.warning(
            "pr_conflict resolve: GitHub call failed for %s#%s",
            repo, pr_number, exc_info=True,
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "pr_conflict resolve: GitHub returned %s for %s#%s",
            resp.status_code, repo, pr_number,
        )
        return None
    body = resp.json()
    if ((body.get("head") or {}).get("sha")) != head_sha:
        return None  # view head is stale; resolve on a later poll
    mergeable = body.get("mergeable")
    if mergeable is None:
        return None  # GitHub still computing; the caller's next poll retries
    return not mergeable


@router.get("/{task_id}/mergeability", response_model=MergeabilityResponse)
async def get_task_mergeability(
    task_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> MergeabilityResponse:
    """Return the ``task_mergeability`` row for a task per ADR-0013.

    The VIEW joins ``task_prs``, so a task with no PR has no row in
    ``task_mergeability``. We surface that as ``derived_mergeability =
    'pending'`` with every other field NULL — the auto-merge orchestrator
    treats it the same as "head sha unknown".

    Lazy conflict resolution (task 536bf319): the VIEW's
    ``pr_conflicting`` reads ``github.pr_conflict`` events, whose only
    producer (the conflict-detection sweep) was deleted in ADR-0087
    Phase 5 — the column could never resolve and coordinators burned
    their full poll budget on NULL. When the VIEW shows NULL conflict
    state at a known head, this endpoint asks GitHub's REST API once
    and persists the DEFINITIVE answer (``is_conflicting`` true OR
    false — false is the clean signal that never existed before) as the
    canonical event, then re-reads the VIEW. GitHub computes
    mergeability asynchronously, so a ``mergeable: null`` reply writes
    nothing — the coordinator's existing 10s poll loop is the retry
    driver, and the GET stays read-only in steady state (a non-NULL
    column short-circuits before any GitHub call).
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

    github_client = getattr(request.app.state, "github_client", None)
    if (
        row.pr_conflicting is None
        and row.head_sha is not None
        and row.repo is not None
        and row.pr_number is not None
        and github_client is not None
    ):
        is_conflicting = await _fetch_github_mergeable(
            github_client, row.repo, row.pr_number, row.head_sha,
        )
        if is_conflicting is not None:
            await dispatcher.persist_and_publish(
                session,
                entity_type="github",
                action="pr_conflict",
                payload=GithubPrConflict(
                    repo=row.repo,
                    pr_number=row.pr_number,
                    head_sha=row.head_sha,
                    is_conflicting=is_conflicting,
                ),
                task_id=row.task_id,
                commit_sha=row.head_sha,
            )
            await session.commit()
            row = (
                await session.execute(
                    text(_MERGEABILITY_SQL),
                    {"id": task_id},
                )
            ).one_or_none()

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
    """Operator-driven retry of a stuck task (ADR-0046, reshaped by ADR-0087).

    Pre-ADR-0087 this endpoint inferred the failed workflow, cleared
    dispatch-dedup rows, checked the per-workflow cap, and created a
    fresh WorkflowRun. All of that machinery is gone (Phase 4 dropped
    the tables). The ADR-0087 shape: emit a ``task.retry`` audit event
    plus a ``task.registered`` event; the repo's coordinator receives
    ``task.registered`` over its WS subscription, re-evaluates the
    task's dependencies, and re-dispatches a worker if unblocked (the
    coordinator's CLAUDE.md §3.2 handler).

    ``body.workflow_id`` and ``body.force_bypass_cap`` are accepted for
    wire-compat but ignored — workflow selection and rework caps are
    coordinator decisions now.
    """
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )

    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="retry",
        payload=TaskRetry(
            workflow_id=body.workflow_id or "coordinator-routed",
            reason=body.reason,
            by_operator="operator",
            bypassed_cap=body.force_bypass_cap,
            previous_run_id=None,
        ),
        plan_id=task.plan_id,
        task_id=task_id,
    )
    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="registered",
        payload=TaskRegistered(
            repo=task.repo,
            title=task.title,
            plan_id=task.plan_id,
        ),
        plan_id=task.plan_id,
        task_id=task_id,
    )
    await session.commit()

    return TaskRetryResponse(workflow_run_id=None)


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
