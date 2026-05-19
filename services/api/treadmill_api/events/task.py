"""Task lifecycle events."""

from __future__ import annotations

import uuid
from typing import ClassVar

import pydantic

from treadmill_api.events.base import EventPayload


class TaskEscalatedToOperator(EventPayload):
    """Emitted when wf-architecture-resolve hits its per-task dispatch cap.

    Per ADR-0048 §3 escalation 3: when the 5-attempt architect cap
    (ADR-0029 Q29.e) blocks dispatch, automated recovery is exhausted.
    Operator intervention is required. Surface via
    GET /api/v1/tasks?status=needs_operator.
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "escalated_to_operator"

    task_id: uuid.UUID
    repo: str
    last_verdict: str | None = None
    last_reasoning: str | None = None
    run_ids: list[str] = pydantic.Field(default_factory=list)


class TaskRegistered(EventPayload):
    """Emitted when a task is created via the API.

    The task is in the *registered* state — not yet ready for dispatch
    until a ``TaskReady`` event fires (or the task is auto-readied because
    its plan is in ``active`` and it has no unsatisfied dependencies).
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "registered"

    repo: str
    title: str
    workflow_version_id: uuid.UUID
    plan_id: uuid.UUID


class TaskReady(EventPayload):
    """Emitted when a task transitions from ``registered`` to ``ready``.

    Dispatched when dependencies are satisfied or when the user explicitly
    marks the task ready (``--ready`` on submission, per ADR-0010)."""

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "ready"


class TaskCancelled(EventPayload):
    """Emitted when a task is cancelled. Cancellation is terminal — no
    workflow runs may be dispatched against the task afterward."""

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "cancelled"

    reason: str | None = None


class TaskRetry(EventPayload):
    """Emitted when an operator retries a task via the CLI (ADR-0046)."""

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "retry"

    workflow_id: str
    reason: str = pydantic.Field(min_length=1, max_length=500)
    by_operator: str
    bypassed_cap: bool
    previous_run_id: str | None = None


class TaskAutoMerged(EventPayload):
    """Emitted when a task's PR is successfully auto-merged via the
    cooling-off trigger (ADR-0031)."""

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "auto_merged"

    merged_sha: str
    pr_number: int
    repo: str
