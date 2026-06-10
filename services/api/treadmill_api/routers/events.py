"""Coordinator-facing manual-event router (ADR-0086 §12.4 Path B prerequisite).

ADR-0086 §12.4 names two paths for the coordinator to learn that a PR
merged: Path A — wait for the GitHub webhook to fire ``github.pr_merged``
through the normal ``webhooks/persist.py`` seam; Path B — when the
webhook drops (network blip, GitHub-side replay, etc.), the coordinator
calls ``POST /api/v1/events`` to manually fire the same lifecycle event
+ then ``PATCH /api/v1/workflow_run_steps/{step_id}`` to mark the step
completed. Both paths produce one canonical row in the ``events`` table;
nothing downstream needs to know which path fired.

This router is the Path B HTTP surface. The persistence seam is
:meth:`Dispatcher.persist_and_publish` so the row matches webhook-sourced
events byte-for-byte (same writer, same column shape, same publish hop
— see ``webhooks/persist.py`` for the Path A counterpart).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher
from treadmill_api.models import Event, Task


router = APIRouter(prefix="/api/v1", tags=["events"])


class CreateEventRequest(BaseModel):
    """POST body for ``/api/v1/events``."""

    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(..., min_length=1, max_length=32)
    action: str = Field(..., min_length=1, max_length=64)
    task_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventResponse(BaseModel):
    """The persisted ``events`` row, returned 201 on success."""

    id: uuid.UUID
    entity_type: str
    action: str
    task_id: uuid.UUID | None
    plan_id: uuid.UUID | None
    payload: dict[str, Any]


@router.post(
    "/events",
    response_model=EventResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_event(
    body: CreateEventRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> EventResponse:
    """Manually fire an events-table row from a non-webhook origin.

    ADR-0086 §12.4 Path B: when the GitHub webhook drops a
    ``pr_merged`` and the coordinator confirms the merge via ``gh pr
    view``, this endpoint is how the coordinator backfills the
    canonical row.

    Behavior

    * 404 if ``task_id`` is supplied but does not resolve in ``tasks``.
      Plan FK is not enforced here (no current caller cares); add when
      a Path B emitter needs it.
    * 409 if a ``github.*`` event with the same ``(entity_type,
      action, task_id)`` already exists. The dedup guard is SCOPED to
      ``entity_type='github'`` — its purpose is Path A (webhook) vs
      Path B (manual backfill) idempotency on canonical GitHub facts,
      where the triple identifies one real-world occurrence. The check
      avoids hashing the payload because the two paths legitimately
      carry slightly different shapes (e.g. the webhook includes the
      GitHub action username; the manual fire may not).

      Non-github audit/observation events (``task.ci_result``,
      ``task.peer_review_verdict``, ``task.routing_pattern_observation``,
      …) repeat by nature — multi-cycle reviews produce one verdict per
      round against the same task — so they are NOT deduped. Round /
      cycle discriminators belong in the payload, not in invented
      action-name variants (which is what coordinators resorted to
      while the guard was unscoped — 2026-06-10).
    * 201 + the persisted row on success.

    The event is persisted via :meth:`Dispatcher.persist_and_publish`
    so the row + publish hop match webhook-sourced events
    (``webhooks/persist.py``). Publish failures are swallowed by the
    helper — the row is the source of truth and the coordinator reads
    it directly via the task board (no consumer-side dependency).
    """
    if body.task_id is not None:
        task = await session.get(Task, body.task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"task {body.task_id!s} not found",
            )

    # Idempotency guard — github.* ONLY: same (entity_type, action,
    # task_id) → 409. Path A (webhook) and Path B (manual backfill)
    # can both record the same canonical GitHub fact; the triple
    # identifies one real-world occurrence, so a retry after a
    # successful-but-slow first call must not produce a second row.
    # Audit/observation events (task.*) repeat by nature (one
    # peer-review verdict per round, multiple ci_results per task) and
    # are deliberately NOT deduped — see the route docstring.
    if body.entity_type == "github":
        existing = await session.execute(
            select(Event).where(
                Event.entity_type == body.entity_type,
                Event.action == body.action,
                Event.task_id == body.task_id,
            )
        )
        duplicate = existing.scalar_one_or_none()
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"event ({body.entity_type!r}, {body.action!r}, "
                    f"task_id={body.task_id!s}) already exists with id "
                    f"{duplicate.id!s}; coordinators should not "
                    "double-fire the same canonical GitHub event"
                ),
            )

    event = await dispatcher.persist_and_publish(
        session,
        entity_type=body.entity_type,
        action=body.action,
        payload=body.payload,
        plan_id=body.plan_id,
        task_id=body.task_id,
    )
    await session.commit()
    return EventResponse(
        id=event.id,
        entity_type=event.entity_type,
        action=event.action,
        task_id=event.task_id,
        plan_id=event.plan_id,
        payload=event.payload or {},
    )
