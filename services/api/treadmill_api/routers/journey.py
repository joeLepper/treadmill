"""``/api/v1/tasks/{task_id}/journey`` — the loop story for one task.

Merges the three real sources the dashboard's loop view needs into one
time-ordered cycle list:

* ``task_executions`` — the worker RUNS (initial + reworks), with their
  attributed token cost from ``llm_calls`` (summed per execution).
* ``events`` — the GATE cycles: ``ci_result`` (CI), ``peer_review_verdict``
  (review), ``evaluator_verdict`` (eval), ``pr_merged`` (merge).

Each cycle carries its kind / outcome / actor / timing / detail and (for
worker runs) raw token sums — the client prices tokens via its pricing
table so pricing stays in one place. Read-only; built for the loop-detail
page, the tasks-board journey bars, and a worker's contributions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import Event, LLMCall, Task, TaskExecution

router = APIRouter(prefix="/api/v1", tags=["journey"])

_TRIGGER_LABEL = {
    "initial": "initial implementation",
    "coordinator-rework": "coordinator-rework",
    "evaluator-rework": "evaluator-rework",
    "peer-review": "peer review",
}


class JourneyCycle(BaseModel):
    kind: str  # dispatch | ci | review | eval | merge
    outcome: str  # pass | fail | lgtm | changes | approve | rework | merged | running
    label: str
    actor: str
    started_at: datetime
    completed_at: datetime | None = None
    detail: str | None = None
    task_execution_id: uuid.UUID | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    model: str | None = None


class TaskJourney(BaseModel):
    task_id: uuid.UUID
    cycles: list[JourneyCycle]


def _exec_cycle(ex: TaskExecution, toks: dict[uuid.UUID, dict[str, Any]]) -> JourneyCycle:
    is_rework = "rework" in ex.trigger
    kind = "review" if (is_rework or ex.trigger == "peer-review") else "dispatch"
    if is_rework:
        outcome = "rework"
    elif ex.status == "failed":
        outcome = "fail"
    elif ex.status == "running":
        outcome = "running"
    else:
        outcome = "pass"
    t = toks.get(ex.id, {})
    return JourneyCycle(
        kind=kind,
        outcome=outcome,
        label=_TRIGGER_LABEL.get(ex.trigger, ex.trigger),
        actor=ex.worker_label,
        started_at=ex.started_at,
        completed_at=ex.completed_at,
        detail=ex.failure_reason,
        task_execution_id=ex.id,
        input_tokens=int(t.get("input", 0)),
        output_tokens=int(t.get("output", 0)),
        cache_read_tokens=int(t.get("cache_read", 0)),
        model=t.get("model"),
    )


def _event_cycle(ev: Event) -> JourneyCycle | None:
    p: dict[str, Any] = ev.payload or {}
    a = ev.action
    if "ci_result" in a:
        ok = p.get("conclusion") == "success"
        return JourneyCycle(
            kind="ci", outcome="pass" if ok else "fail", label="CI", actor="github",
            started_at=ev.created_at, completed_at=ev.created_at,
            detail=f"{p.get('app_slug', 'CI')}: {p.get('conclusion', '?')}",
        )
    if "peer_review_verdict" in a:
        v = str(p.get("verdict", "")).lower()
        ok = v in {"lgtm", "approve", "approved"}
        return JourneyCycle(
            kind="review", outcome="lgtm" if ok else "changes", label="peer review",
            actor=str(p.get("reviewer", "peer")), started_at=ev.created_at,
            completed_at=ev.created_at, detail=p.get("note"),
        )
    if "evaluator_verdict" in a:
        v = str(p.get("verdict", "")).lower()
        return JourneyCycle(
            kind="eval", outcome="approve" if v == "approve" else "changes",
            label="evaluator", actor=str(p.get("evaluator", "evaluator")),
            started_at=ev.created_at, completed_at=ev.created_at, detail=p.get("note"),
        )
    if a == "pr_merged" or a.endswith("pr_merged"):
        pr = p.get("pr_number")
        return JourneyCycle(
            kind="merge", outcome="merged",
            label=f"merged #{pr}" if pr else "merged", actor="coordinator",
            started_at=ev.created_at, completed_at=ev.created_at,
            detail=p.get("head_branch"),
        )
    return None


@router.get("/tasks/{task_id}/journey", response_model=TaskJourney)
async def get_task_journey(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskJourney:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"task {task_id!s} not found")

    execs = (
        await session.execute(
            select(TaskExecution).where(TaskExecution.task_id == task_id).order_by(TaskExecution.started_at)
        )
    ).scalars().all()

    toks: dict[uuid.UUID, dict[str, Any]] = {}
    exec_ids = [e.id for e in execs]
    if exec_ids:
        rows = await session.execute(
            select(
                LLMCall.task_execution_id,
                func.sum(LLMCall.input_tokens),
                func.sum(LLMCall.output_tokens),
                func.sum(func.coalesce(LLMCall.cache_read_tokens, 0)),
                func.min(LLMCall.model),
            )
            .where(LLMCall.task_execution_id.in_(exec_ids))
            .group_by(LLMCall.task_execution_id)
        )
        for eid, inp, out, cr, model in rows.all():
            toks[eid] = {"input": inp, "output": out, "cache_read": cr, "model": model}

    events = (
        await session.execute(
            select(Event).where(Event.task_id == task_id).order_by(Event.created_at)
        )
    ).scalars().all()

    cycles: list[JourneyCycle] = [_exec_cycle(e, toks) for e in execs]
    for ev in events:
        c = _event_cycle(ev)
        if c is not None:
            cycles.append(c)
    cycles.sort(key=lambda c: c.started_at)
    return TaskJourney(task_id=task_id, cycles=cycles)
