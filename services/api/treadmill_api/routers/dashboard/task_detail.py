"""``GET /api/v1/dashboard/tasks/{task_id}`` — Task-detail bundle.

This endpoint matches ``services/dashboard/src/api/queries.ts``
``useTaskDetail``'s ``queryFn`` shape exactly:

    { task, runs }

* ``task`` mirrors a single row from ``/overview``'s ``tasks`` array
  (same fields, same nullability) so the detail page can render its
  chrome from one payload.
* ``runs`` is the full, flat list of ``Run`` objects for the task —
  each carries its own ``steps``. Iteration derivation (the "ci-fix
  loop #N" stepper) is the client's job: ``deriveIterations(runs)``
  in ``services/dashboard/src/api/mock.ts`` is the reference. Doing
  it client-side keeps the contract symmetric across mock + live
  data and lets the page change iteration kinds without an API
  redeploy.

Returns 404 when the task does not exist.

Per-run / per-step derivations:

  * Run status is derived from its steps — ``failed`` if any step is
    ``failed``, ``running`` if any is ``running``, ``completed`` when
    every step is ``completed``, ``queued`` when every step is still
    ``pending``, otherwise ``running``. ``workflow_runs`` itself
    carries no status column (ADR-0010 / ADR-0011: step.status is the
    single mutable projection).
  * Run ``started_at`` is the earliest step ``started_at``, falling
    back to ``workflow_runs.created_at`` (e.g. queued runs with no
    started step yet).
  * Run ``completed_at`` is the latest step ``completed_at`` when the
    derived status is terminal, else ``None`` (running / queued runs
    have no completion timestamp yet).
  * Run ``duration_s`` is ``completed_at - started_at`` in whole
    seconds, or ``None`` for in-flight runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.dashboard.overview import (
    PipelineStep,
    PullRequest,
    Task,
)


router = APIRouter()


# ── Response shapes ───────────────────────────────────────────────────────────
# Field names match ``services/dashboard/src/api/types.ts`` 1:1 (``Run``,
# ``RunStep``, ``StepOutput``, ``TaskDetail``). ``tokens`` is a plain
# ``dict[str, int]`` because the TS shape ``{ in, out }`` collides with
# Python's ``in`` keyword — modeling it as a dict keeps the wire payload
# verbatim without alias plumbing.


class StepOutput(BaseModel):
    summary: str | None = None
    decision: str | None = None
    commit_sha: str | None = None


class RunStep(BaseModel):
    id: str
    role_id: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_s: int | None
    output: StepOutput | None
    error: str | None = None
    tokens: dict[str, int]


class Run(BaseModel):
    id: str
    workflow_id: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    duration_s: int | None
    steps: list[RunStep]


class TaskDetailResponse(BaseModel):
    task: Task
    runs: list[Run]


# ── Step → run status derivation ─────────────────────────────────────────────


def _derive_run_status(step_statuses: list[str]) -> str:
    """Run status from its step statuses.

    Mirrors the "the failing step wins; otherwise the latest one"
    convention the dashboard's iteration stepper already encodes
    client-side (mock.ts ``deriveIterations`` finalize loop).
    """
    if not step_statuses:
        return "queued"
    if any(s == "failed" for s in step_statuses):
        return "failed"
    if any(s == "running" for s in step_statuses):
        return "running"
    if all(s == "completed" for s in step_statuses):
        return "completed"
    if all(s == "pending" for s in step_statuses):
        return "queued"
    return "running"


# ── SQL ──────────────────────────────────────────────────────────────────────


_TASK_SQL = """
SELECT
    t.id::text                       AS id,
    t.title                          AS title,
    t.repo                           AS repo,
    t.plan_id::text                  AS plan_id,
    t.created_at                     AS created_at,
    ts.derived_status                AS derived_status,
    rc.mode                          AS repo_mode,
    rc.claude_account                AS claude_account,
    tp.pr_number                     AS pr_number,
    tp.branch                        AS pr_branch,
    tm.head_sha                      AS pr_head_sha,
    tm.ci_conclusion                 AS pr_ci_conclusion,
    tm.review_decision               AS pr_review_decision,
    tm.validate_decision             AS pr_validate_decision,
    tm.pr_conflicting                AS pr_conflicting,
    tm.derived_mergeability          AS pr_derived_mergeability,
    latest_run.run_id                AS latest_run_id,
    latest_run.workflow_id           AS latest_workflow_id,
    latest_run.started_at            AS latest_run_started_at,
    COALESCE(last_event.created_at, t.created_at) AS last_activity,
    COALESCE(token_rollup.tokens_total, 0)        AS tokens_total
FROM tasks t
LEFT JOIN task_status      ts ON ts.id      = t.id
LEFT JOIN repo_configs     rc ON rc.repo    = t.repo
LEFT JOIN task_prs         tp ON tp.task_id = t.id
LEFT JOIN task_mergeability tm ON tm.task_id = t.id
LEFT JOIN LATERAL (
    SELECT r.id AS run_id, wv.workflow_id, r.created_at AS started_at
    FROM workflow_runs r
    JOIN workflow_versions wv ON wv.id = r.workflow_version_id
    WHERE r.task_id = t.id
    ORDER BY r.created_at DESC
    LIMIT 1
) latest_run ON TRUE
LEFT JOIN LATERAL (
    SELECT MAX(e.created_at) AS created_at
    FROM events e
    WHERE e.task_id = t.id
) last_event ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(
        COALESCE(s.input_tokens, 0)
      + COALESCE(s.output_tokens, 0)
    ) AS tokens_total
    FROM workflow_run_steps s
    JOIN workflow_runs r ON r.id = s.run_id
    WHERE r.task_id = t.id
) token_rollup ON TRUE
WHERE t.id = :task_id
"""


_PIPELINE_SQL = """
SELECT
    s.role_id    AS role,
    s.status     AS status,
    s.step_index AS step_index
FROM workflow_run_steps s
WHERE s.run_id = :run_id
ORDER BY s.step_index
"""


_ESCALATION_SQL = """
-- Mirrors overview.py's escalation derivation for a single task: a task
-- is escalated iff its most recent ``escalated_to_operator`` event has
-- not been followed by an ``escalation_acknowledged``.
WITH last_escalation AS (
    SELECT created_at, payload->>'reason' AS reason
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalated_to_operator'
      AND task_id = :task_id
    ORDER BY created_at DESC
    LIMIT 1
),
last_ack AS (
    SELECT created_at
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalation_acknowledged'
      AND task_id = :task_id
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT le.created_at AS escalated_at, le.reason AS reason
FROM last_escalation le
LEFT JOIN last_ack la ON TRUE
WHERE la.created_at IS NULL OR la.created_at < le.created_at
"""


_RUNS_SQL = """
SELECT
    r.id::text          AS id,
    wv.workflow_id      AS workflow_id,
    r.created_at        AS created_at
FROM workflow_runs r
JOIN workflow_versions wv ON wv.id = r.workflow_version_id
WHERE r.task_id = :task_id
ORDER BY r.created_at ASC
"""


_RUN_STEPS_SQL = """
SELECT
    s.id::text          AS id,
    s.run_id::text      AS run_id,
    s.role_id           AS role_id,
    s.status            AS status,
    s.started_at        AS started_at,
    s.completed_at      AS completed_at,
    s.output            AS output,
    s.error             AS error,
    s.input_tokens      AS input_tokens,
    s.output_tokens     AS output_tokens,
    s.step_index        AS step_index
FROM workflow_run_steps s
WHERE s.run_id = ANY(:run_ids)
ORDER BY s.run_id, s.step_index
"""


# Match overview.py's mapping (mock.ts uses "done" for completed pipeline cells;
# RunStep status passes through unmapped — types.ts allows raw status enums).
_PIPELINE_STATUS_MAP = {"completed": "done"}


# Token-cost estimate ratio mirrors overview.py so the detail-page cost
# matches the row on /overview down to the cent.
_USD_PER_TOKEN = 14.79 / 1_000_000


def _step_output(raw: Any) -> StepOutput | None:
    if not isinstance(raw, dict):
        return None
    summary = raw.get("summary")
    decision = raw.get("decision")
    commit_sha = raw.get("commit_sha")
    if summary is None and decision is None and commit_sha is None:
        return None
    return StepOutput(
        summary=summary if isinstance(summary, str) else None,
        decision=decision if isinstance(decision, str) else None,
        commit_sha=commit_sha if isinstance(commit_sha, str) else None,
    )


def _duration_seconds(
    started: datetime | None, completed: datetime | None,
) -> int | None:
    if started is None or completed is None:
        return None
    return int((completed - started).total_seconds())


# ── Endpoint ─────────────────────────────────────────────────────────────────


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
async def get_task_detail(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskDetailResponse:
    """Return ``{ task, runs }`` for one task.

    404 when the task does not exist. A task with no runs returns
    ``runs: []`` (registered tasks the scheduler hasn't dispatched yet
    legitimately have zero rows in ``workflow_runs``).
    """
    task_row = (
        await session.execute(text(_TASK_SQL), {"task_id": task_id})
    ).mappings().one_or_none()
    if task_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found",
        )

    # Escalation (single-task projection of overview.py's logic).
    escalation_row = (
        await session.execute(text(_ESCALATION_SQL), {"task_id": task_id})
    ).mappings().one_or_none()
    escalated = escalation_row is not None
    escalation_reason: str | None = (
        escalation_row["reason"] if escalation_row else None
    )

    # Pipeline strip for the task header — same shape as overview.py.
    pipeline: list[PipelineStep] = []
    latest_run_id = task_row["latest_run_id"]
    if latest_run_id is not None:
        step_rows = (
            await session.execute(text(_PIPELINE_SQL), {"run_id": latest_run_id})
        ).mappings().all()
        for srow in step_rows:
            pipeline.append(
                PipelineStep(
                    role=srow["role"],
                    status=_PIPELINE_STATUS_MAP.get(srow["status"], srow["status"]),
                )
            )

    pr: PullRequest | None = None
    if task_row["pr_number"] is not None:
        pr = PullRequest(
            pr_number=task_row["pr_number"],
            branch=task_row["pr_branch"] or "",
            head_sha=(task_row["pr_head_sha"] or "")[:7],
            ci_conclusion=task_row["pr_ci_conclusion"],
            review_decision=task_row["pr_review_decision"],
            validate_decision=task_row["pr_validate_decision"],
            pr_conflicting=bool(task_row["pr_conflicting"]),
            derived_mergeability=task_row["pr_derived_mergeability"] or "pending",
        )

    tokens_total = int(task_row["tokens_total"] or 0)
    task = Task(
        id=task_row["id"],
        title=task_row["title"],
        repo=task_row["repo"],
        repo_mode=task_row["repo_mode"] or "conform",
        account=task_row["claude_account"] or "default",
        plan_id=task_row["plan_id"],
        derived_status=task_row["derived_status"] or "registered",
        last_activity=task_row["last_activity"],
        started_at=task_row["latest_run_started_at"],
        created_at=task_row["created_at"],
        pipeline=pipeline,
        workflow=task_row["latest_workflow_id"],
        pr=pr,
        escalated=escalated,
        escalation_reason=escalation_reason,
        cost_usd=round(tokens_total * _USD_PER_TOKEN, 2),
        tokens=tokens_total,
    )

    # Runs — chronological, with their steps. Two queries (runs + bulk
    # steps) keeps this O(2) regardless of run count.
    run_rows = (
        await session.execute(text(_RUNS_SQL), {"task_id": task_id})
    ).mappings().all()

    runs: list[Run] = []
    if run_rows:
        run_ids = [row["id"] for row in run_rows]
        step_rows = (
            await session.execute(
                text(_RUN_STEPS_SQL), {"run_ids": run_ids},
            )
        ).mappings().all()
        steps_by_run: dict[str, list[RunStep]] = {}
        for srow in step_rows:
            in_tok = int(srow["input_tokens"] or 0)
            out_tok = int(srow["output_tokens"] or 0)
            steps_by_run.setdefault(srow["run_id"], []).append(
                RunStep(
                    id=srow["id"],
                    role_id=srow["role_id"],
                    status=srow["status"],
                    started_at=srow["started_at"],
                    completed_at=srow["completed_at"],
                    duration_s=_duration_seconds(
                        srow["started_at"], srow["completed_at"],
                    ),
                    output=_step_output(srow["output"]),
                    error=srow["error"],
                    tokens={"in": in_tok, "out": out_tok},
                )
            )

        for row in run_rows:
            run_id = row["id"]
            steps = steps_by_run.get(run_id, [])
            statuses = [s.status for s in steps]
            run_status = _derive_run_status(statuses)
            step_starts = [s.started_at for s in steps if s.started_at is not None]
            step_ends = [s.completed_at for s in steps if s.completed_at is not None]
            started_at = min(step_starts) if step_starts else row["created_at"]
            if run_status in ("completed", "failed") and step_ends:
                completed_at: datetime | None = max(step_ends)
            else:
                completed_at = None
            runs.append(
                Run(
                    id=run_id,
                    workflow_id=row["workflow_id"],
                    status=run_status,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_s=_duration_seconds(started_at, completed_at),
                    steps=steps,
                )
            )

    return TaskDetailResponse(task=task, runs=runs)
