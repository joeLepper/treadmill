"""Task lifecycle events."""

from __future__ import annotations

import uuid
from typing import ClassVar

from treadmill_api.events.base import EventPayload


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


class TaskAutoMerged(EventPayload):
    """Emitted when a task's PR is successfully auto-merged via the
    cooling-off trigger (ADR-0031)."""

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "auto_merged"

    merged_sha: str
    pr_number: int
    repo: str
