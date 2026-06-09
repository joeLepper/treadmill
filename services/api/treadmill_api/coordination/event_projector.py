"""EventProjector — ADR-0011 single-writer for state projection.

Extracted from ``coordination/consumer.py`` per the Phase-3C plan.
This class owns *every* DB write that constitutes the canonical
projection of an event onto authoritative state:

  * Event audit rows (``events`` table) via :meth:`persist_audit_row`
  * Step status transitions (``workflow_run_steps``) via
    :meth:`apply_step_status`
  * Task PR bridge rows (``task_prs``) via :meth:`write_task_prs`

It holds **no client dependencies** — every write is parameterised on
the ``AsyncSession`` passed in. The router or consumer that calls into
the projector owns the transaction boundary.

The drain of webhook events buffered against a PR (D.8) stays out of
the projector — that's a routing concern (it emits new SQS messages
that come back through ``handle()`` and re-enter the projector). The
projector returns the (task_id, repo) tuple from :meth:`write_task_prs`
so the caller can run the drain after the projection commits.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.events.step import (
    StepCancelled,
    StepCompleted,
    StepFailed,
    StepSkipped,
    StepStarted,
)
from treadmill_api.models import Event, Task, TaskPR, WorkflowRun, WorkflowRunStep

logger = logging.getLogger("treadmill.coordination.projector")


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class TaskPRWritten:
    """Returned by :meth:`EventProjector.write_task_prs` when an INSERT lands.

    The caller (PlanRouter) uses this to fire ``drain_pending_events`` after
    the projection commits. Returns ``None`` from the projector method when
    the step did not author a PR (no-op).
    """

    task_id: uuid.UUID
    repo: str
    pr_number: int


class EventProjector:
    """Pure-projection writer. No client deps, no side-effects beyond the
    session passed in. Safe to share across consumer + router instances.
    """

    async def persist_audit_row(
        self,
        session: AsyncSession,
        record: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """INSERT the Event row idempotently. Pre-existing (event_id)
        rows — typically dispatcher-origin events — are left untouched."""
        raw_id = record.get("event_id")
        if not raw_id:
            logger.debug(
                "event without event_id; skipping audit INSERT (%s.%s)",
                record.get("entity_type"), record.get("action"),
            )
            return
        try:
            event_id = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            logger.warning("malformed event_id in record: %r", raw_id)
            return

        stmt = (
            pg_insert(Event)
            .values(
                id=event_id,
                entity_type=record.get("entity_type"),
                action=record.get("action"),
                plan_id=_uuid_or_none(record.get("plan_id")),
                task_id=_uuid_or_none(record.get("task_id")),
                run_id=_uuid_or_none(record.get("run_id")),
                step_id=_uuid_or_none(record.get("step_id")),
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.execute(stmt)

    async def apply_step_status(
        self,
        session: AsyncSession,
        action: str | None,
        step_id: str,
        typed: Any,
        payload: dict[str, Any],
    ) -> bool:
        """Apply the validated typed step event to ``workflow_run_steps``.

        Each ``UPDATE`` is gated on the prior status — the WHERE clause
        is the idempotency mechanism for re-delivery. See the consumer
        module docstring for the full transition table.
        """
        if action == "started":
            assert isinstance(typed, StepStarted)
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status == "pending",
                )
                .values(status="running", started_at=typed.started_at)
            )
            return True
        if action == "completed":
            assert isinstance(typed, StepCompleted)
            output_to_store = typed.output.model_dump(mode="json")
            usage = typed.token_usage
            values: dict[str, Any] = {
                "status": "completed",
                "completed_at": typed.completed_at,
                "output": output_to_store,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "cache_creation_tokens": (
                    usage.cache_creation_tokens if usage else None
                ),
                "cache_read_tokens": usage.cache_read_tokens if usage else None,
                "model": usage.model if usage else None,
            }
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status.in_(("pending", "running")),
                )
                .values(**values)
            )
            return True
        if action == "failed":
            assert isinstance(typed, StepFailed)
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status.in_(("pending", "running")),
                )
                .values(
                    status="failed",
                    completed_at=typed.failed_at,
                    error=typed.error,
                )
            )
            return True
        if action == "cancelled":
            assert isinstance(typed, StepCancelled)
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status == "pending",
                )
                .values(status="cancelled")
            )
            return True
        if action == "skipped":
            assert isinstance(typed, StepSkipped)
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status == "pending",
                )
                .values(status="skipped")
            )
            return True
        logger.debug("coordination projector ignoring step.%s", action)
        return False

    async def write_task_prs(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
        payload: dict[str, Any],
    ) -> TaskPRWritten | None:
        """Insert a ``task_prs`` row when a step completes with a PR.

        Returns ``TaskPRWritten`` so the caller can drain webhook events
        buffered against that (repo, pr_number) — drain is a routing
        concern and stays outside the projector.

        Defense against payload spoofing: the ``repo`` we write is the
        *task's* stored ``tasks.repo``, never the worker-reported value.
        """
        if not isinstance(typed, StepCompleted):
            return None
        envelope = typed.output
        pr_number_raw = envelope.payload.get("pr_number")
        if pr_number_raw is None:
            return None
        if not isinstance(pr_number_raw, int) or isinstance(pr_number_raw, bool):
            logger.warning(
                "task_prs write: payload.pr_number not an int for step %s; "
                "skipping (got %r)",
                step_id, pr_number_raw,
            )
            return None
        pr_number = pr_number_raw

        branch: str | None = None
        for artifact in envelope.artifacts:
            if artifact.kind == "branch":
                branch = artifact.value
                break
        if branch is None:
            logger.warning(
                "task_prs write: no branch artifact for step %s (pr_number=%d); "
                "skipping",
                step_id, pr_number,
            )
            return None

        result = await session.execute(
            select(WorkflowRun.task_id, Task.repo)
            .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
            .join(Task, Task.id == WorkflowRun.task_id)
            .where(WorkflowRunStep.id == step_id)
        )
        row = result.first()
        if row is None:
            logger.warning(
                "task_prs write: no task found for step_id=%s; skipping", step_id,
            )
            return None
        task_id = row.task_id
        repo = row.repo

        stmt = (
            pg_insert(TaskPR)
            .values(
                repo=repo,
                pr_number=pr_number,
                task_id=task_id,
                branch=branch,
            )
            .on_conflict_do_nothing(index_elements=["repo", "pr_number"])
        )
        await session.execute(stmt)
        logger.info(
            "task_prs row written: repo=%s pr_number=%d task_id=%s",
            repo, pr_number, task_id,
        )
        return TaskPRWritten(task_id=task_id, repo=repo, pr_number=pr_number)
