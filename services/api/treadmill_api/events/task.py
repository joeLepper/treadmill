"""Task lifecycle events."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar, Literal

import pydantic

from treadmill_api.events.base import EventPayload


class TaskEscalatedToOperator(EventPayload):
    """Emitted when automated recovery is exhausted and operator
    intervention is required.

    Five call sites today (the ``reason`` field discriminates):
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
      * ``terminal_step_failure`` — per ADR-0062, a ``step.failed``
        landed on a workflow run with no further pending steps and
        no concurrent cap-reached escalation. ``step_name`` is set
        to the failing step's name and ``gate_log_excerpt`` carries
        the step's captured error / log excerpt when present.
      * ``terminal_gate_sweep`` — per ADR-0047, ADR-0038, ADR-0042,
        the scheduled sweep detected a task with an architect
        accept-as-is verdict (``review.override`` / ``validate.override``)
        whose PR was never merged; see
        ``terminal_gate_sweep.run_terminal_gate_sweep``.
      * ``step_starvation`` — per ADR-0075, the scheduled sweep detected
        a step queued for dispatch (``step.ready``) that never reached
        execution (``step.started``); see
        ``step_starvation_sweep.run_step_starvation_sweep``.

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
    # ADR-0058 / ADR-0062: distinguish escalation sources so dashboards /
    # sweeps can triage cases separately. Optional with default=None so
    # the existing escalation emitters keep working without an in-place
    # schema migration; the existing emitters set this on the natural
    # deploy cycle.
    reason: Literal[
        "architect_cap",
        "stuck_task_sweep",
        "gate-broken",
        "terminal_step_failure",
        "terminal_gate_sweep",
        "step_starvation",
    ] | None = None
    # ADR-0058: populated for ``reason='gate-broken'`` with the failing
    # deterministic gate's stderr. The architect role copies it from
    # ``source_step.output.payload.validation_results[].log_excerpt``
    # so the operator sees the actual tooling failure on the
    # escalation event without re-running the loop. Capped at 4000
    # chars (same bound as ArchitectVerdict.gate_log_excerpt).
    # ADR-0062 also populates this for ``reason='terminal_step_failure'``
    # from the failing step's captured error / log excerpt.
    gate_log_excerpt: str | None = pydantic.Field(None, max_length=4000)
    # ADR-0062: the failing step's ``step_name`` (e.g. ``action`` /
    # ``analyze``) on ``reason='terminal_step_failure'``. Optional and
    # default-None so the existing emitters that don't carry a single
    # owning step (cap-reached, stuck-task-sweep) remain wire-compatible.
    step_name: str | None = None
    # Spawning operator label (``tasks.created_by``) for dashboard triage.
    # Nullable — older tasks may not carry this; renders as `` by <label>``
    # suffix on the escalation-open notification line when present.
    created_by: str | None = None

    @pydantic.field_validator("created_by", mode="before")
    @classmethod
    def _coerce_created_by(cls, v: object) -> str | None:
        """An escalation must NEVER fail to emit because ``created_by`` isn't a
        clean ``str | None`` — it's best-effort triage metadata. Coerce any
        non-string (a missing column, a stray value) to ``None`` so the
        operator's safety-net event always fires."""
        return v if isinstance(v, str) else None


class TaskEscalationClosed(EventPayload):
    """Emitted when an open operator-escalation incident is closed (ADR-0062).

    Pairs with ``TaskEscalatedToOperator`` as the close side of the
    incident lifecycle. The ``close_reason`` discriminates the five
    close triggers:

      * ``re_progressed`` — a ``step.completed`` event landed for the
        task with ``created_at > opened_at`` (the task is dispatching
        again; the underlying stall is gone).
      * ``pr_merged`` — a ``github.pr_merged`` event exists for the
        task (the change shipped; whatever was blocking the loop is
        resolved by the merge).
      * ``cancelled`` — a ``task.cancelled`` terminal landed.
      * ``superseded`` — a ``task.superseded`` terminal landed (a
        replacement task took over).
      * ``operator_close`` — the CLI ``treadmill escalations close
        <task_id>`` command (Step 3) emitted the close explicitly.

    ``opened_at`` is denormalized from the matching
    ``task.escalated_to_operator`` event so consumers don't need to
    re-join. ``mttr_seconds`` is the wall-clock incident duration
    (``closed_at - opened_at``), computed at emit-time and stamped on
    every close so MTTR aggregation over a window is a simple
    column-scan rather than a paired-event join.

    ``expected_followup`` optionally records what the operator expected
    to happen next. Values are structured: ``learning:<slug>`` (issue a
    learning), ``pr:<number>`` (related PR), ``adr:<NNNN>`` (architectural
    decision record), or ``transient:<cause>`` (temporary issue, no followup).
    Omitted or null values are counted as unreferenced closes by the weekly
    sweep.
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "escalation_closed"

    close_reason: Literal[
        "re_progressed",
        "pr_merged",
        "cancelled",
        "superseded",
        "operator_close",
    ]
    opened_at: datetime
    mttr_seconds: int
    expected_followup: str | None = None


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
    workflow runs may be dispatched against the task afterward.

    ``schedule_id`` + ``cancelled_by`` are populated by the scheduler
    pending-tick coalesce path (a newer scheduled.tick supersedes a
    prior pending tick for the same schedule before dispatch). Both
    default to ``None`` so operator-cancel callers (the dashboard
    cancel route) stay wire-compatible.
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "cancelled"

    reason: str | None = None
    schedule_id: uuid.UUID | None = None
    cancelled_by: str | None = None


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


class TaskWorkerDepsFailed(EventPayload):
    """Emitted when ``repo_deps.materialize`` raises
    ``WorkerDepsMaterializationError`` (ADR-0059 Step 4).

    The operator surface (dashboard escalations list) gets a distinct
    event vs ``gate-broken`` / ``architect_cap`` / ``stuck_task_sweep``
    so a worker-deps registration failure is triaged independently of
    the verdict-driven escalations. The step still fails (the runner's
    materialize-failure path re-raises after publishing this event), so
    ``step.failed`` carries the audit trail while this typed event is
    the operator-visible signal that the repo's ``WorkerDeps``
    registration needs attention.
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "worker_deps_failed"

    task_id: uuid.UUID
    repo: str
    # Matches ``WorkerDepsMaterializationError.stage`` (the three install
    # phases in ``workers/agent/treadmill_agent/repo_deps.py``).
    stage: Literal["python", "node", "binary"]
    # The exception's ``detail`` field — captured stderr or the
    # checksum-mismatch line — so the operator can read the actual
    # failure on the escalation event without re-running the loop.
    detail: str = pydantic.Field(min_length=1)
    # ``compute_deps_hash(worker_deps)`` for cache-correlation: an
    # operator can see whether two failures share a registration shape.
    worker_deps_hash: str = pydantic.Field(min_length=1)


class OperatorHintSet(EventPayload):
    """Emitted when the operator sets or clears an operator_note on a task.

    Per ADR-0081 §1: the operator uses this hint channel to inject context
    into the worker's next step prompt. This event provides an audit trail
    of what hints were set, by whom, and when. Used to measure hint-channel
    usage patterns and feed ADR-0075's fleet-wedge detector (high hint
    request rate = workers getting stuck).
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "operator_hint_set"

    note_excerpt: str = pydantic.Field(min_length=1, max_length=500)
    """First 500 chars of the operator_note for the audit log. When the note
    is null/cleared, this is a marker like '(cleared)' so the event always
    carries a human-readable payload."""
    set_by: str = pydantic.Field(min_length=1)
    """Who set the hint (operator label, CLI user, etc.)."""


class TaskWorkerHintRequested(EventPayload):
    """Emitted when a worker invokes the request_hint tool.

    Per ADR-0081 §2: the worker uses this tool to request operator context
    when stuck. This event provides an audit trail of what hints were
    requested, why, and when. Used to measure hint-channel usage patterns
    and feed ADR-0075's fleet-wedge detector (high hint request rate =
    workers getting stuck).
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "worker_hint_requested"

    reason: str = pydantic.Field(min_length=1, max_length=100)
    """Short slug naming the class of help wanted
    (e.g. tests_need_scope, alembic_heads_unclear)."""
    context_excerpt: str = pydantic.Field(min_length=1, max_length=500)
    """First 500 chars of the context for the audit log."""
    worker_step_id: str = pydantic.Field(min_length=1)
    """The step ID of the worker step that made the request."""


class ArchitectEmitFailure(EventPayload):
    """Emitted when the architect role cannot produce a parseable verdict.

    Per ADR-0083: the architect call uses ``--json-schema`` to constrain
    output. When the CLI's ``structured_output`` field is absent or the
    envelope fails post-emit validation, the worker emits this event and
    returns a synthetic ``emit-failure`` verdict to ``handle()``.

    The API trigger ``maybe_drop_relay_on_architect_emit_failure`` converts
    this event into a cc-relay file drop in the dispatching orchestrator's
    inbox so the operator can triage without polling.
    """

    ENTITY_TYPE: ClassVar[str] = "task"
    ACTION: ClassVar[str] = "architect_emit_failure"

    parse_failure_reason: Literal[
        "no-structured-output",
        "supersede-missing-rewrite",
        "gate-broken-missing-excerpt",
        "invalid-verdict-literal",
    ]
    """Discriminator identifying which post-emit validation check failed."""
    model_output_excerpt: str = pydantic.Field(min_length=0, max_length=4096)
    """First 2 KB of the raw model output + summary, truncated for the relay."""
    created_by: str = pydantic.Field(min_length=1)
    """The dispatching session label (task.created_by). Routes the relay
    drop to ``~/.cc-channels/<created_by>/relay/``."""
    failing_run_id: str = pydantic.Field(min_length=1)
    """UUID of the wf-architecture-resolve run that produced the failure."""
