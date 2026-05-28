"""Task lifecycle events."""

from __future__ import annotations

import uuid
from typing import ClassVar, Literal

import pydantic

from treadmill_api.events.base import EventPayload


class TaskEscalatedToOperator(EventPayload):
    """Emitted when automated recovery is exhausted and operator
    intervention is required.

    Three call sites today (the ``reason`` field discriminates):
      * ``architect_cap`` — per ADR-0048 §3, wf-architecture-resolve hit
        its 5-attempt per-task cap (ADR-0029 Q29.e); see
        ``triggers._emit_arch_cap_reached``.
      * ``stuck_task_sweep`` — per ADR-0047, the scheduled sweep
        detected a non-terminal task with no recent activity; see
        ``stuck_task_sweep.run_stuck_task_sweep``.
      * ``gate-broken`` — per ADR-0058, the architect verdicted
        ``gate-broken`` on a wf-architecture-resolve step (the
        deterministic gate is failing for reasons outside the author's
        control). Carries the gate's stderr in ``gate_log_excerpt``
        so the operator can repair the gate without re-running the
        loop. The amend-cap counter is not incremented because the
        architect's verdict is not ``amend``.

    Surface via GET /api/v1/tasks?status=needs_operator and the
    dashboard's escalation bucket (``routers/dashboard/overview.py``).
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "escalated_to_operator"

    task_id: uuid.UUID
    repo: str
    last_verdict: str | None = None
    last_reasoning: str | None = None
    run_ids: list[str] = pydantic.Field(default_factory=list)
    # ADR-0058: distinguish escalation sources so dashboards / sweeps
    # can triage gate-broken cases separately from cap escalations.
    # Optional with default=None so the existing escalation emitters
    # keep working without an in-place schema migration; the existing
    # emitters set this on the natural deploy cycle.
    reason: Literal["architect_cap", "stuck_task_sweep", "gate-broken"] | None = None
    # ADR-0058: populated for ``reason='gate-broken'`` with the failing
    # deterministic gate's stderr. The architect role copies it from
    # ``source_step.output.payload.validation_results[].log_excerpt``
    # so the operator sees the actual tooling failure on the
    # escalation event without re-running the loop. Capped at 4000
    # chars (same bound as ArchitectVerdict.gate_log_excerpt).
    gate_log_excerpt: str | None = pydantic.Field(None, max_length=4000)


class TaskEscalationAcknowledged(EventPayload):
    """Emitted when an operator acks an outstanding ``escalated_to_operator``.

    Pairs with ``TaskEscalatedToOperator``: the dashboard's escalation
    surface treats a task as escalated only while its most recent
    ``escalated_to_operator`` has no later ``escalation_acknowledged``
    (see ``routers/dashboard/overview.py`` ``_ESCALATIONS_SQL``). Empty
    payload — the row's ``task_id`` + ``created_at`` carry all the
    information the surface needs.
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "escalation_acknowledged"


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
