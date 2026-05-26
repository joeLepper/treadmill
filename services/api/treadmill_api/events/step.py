"""Workflow-run-step lifecycle events.

Steps go through ``ready → started → (completed | failed | cancelled)``;
the consumer that reads these events writes ``workflow_run_steps.status``
per ADR-0011's single-writer projection pattern.

``StepCompleted.output`` carries the uniform ``StepOutput`` envelope
(ADR-0012). The envelope is universal across every role and every
workflow; per-workflow extras live in ``StepOutput.payload`` by
documented convention (ADR-0012 §"Convention map for wf-author's
payload" + ADR-0015 §"Per-workflow shape matrix"). The Week-2-closure
``AuthorStepOutput`` Pydantic class is intentionally absent — its
fields demote into the envelope (``commit_sha`` at top-level, ``branch``
and ``pr_url`` as ``Artifact``s, ``pr_number`` in ``payload``).
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from treadmill_api.events.base import EventPayload
from treadmill_api.events.step_output import StepOutput


class StepTokenUsage(BaseModel):
    """Per-step LLM token counters (ADR-0020, Wave 1).

    Mirrors the ``usage`` block parsed from Claude Code's JSON envelope.
    Sub-model fields are all required so a worker that emits
    ``token_usage`` must emit every counter — the consumer NULLs the
    five DB columns wholesale when ``token_usage`` is absent on the
    outer ``StepCompleted``, so partial sub-model payloads would create
    an ambiguous half-recorded row.

    ``model`` is the role's model id (e.g. ``claude-opus-4-7``) the
    counters are attributable to; persisted alongside so a future
    cost-per-model rollup doesn't have to join through ``roles``.
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    model: str


# ── Step lifecycle event payloads ─────────────────────────────────────────────


class StepReady(EventPayload):
    """A step is ready for a worker to consume.

    Dispatched after the previous step in the run completes (or for the
    first step of a run, when the run is created)."""

    ENTITY_TYPE: ClassVar[str] = "step"
    ACTION: ClassVar[str] = "ready"

    role_id: str
    step_index: int
    step_name: str
    repo: str
    workflow_id: str


class StepStarted(EventPayload):
    """A worker picked up the step and started executing."""

    ENTITY_TYPE: ClassVar[str] = "step"
    ACTION: ClassVar[str] = "started"

    started_at: datetime


class StepCompleted(EventPayload):
    """A worker completed the step successfully.

    The ``output`` field is a uniform ``StepOutput`` envelope (ADR-0012)
    that every role and every workflow conforms to. The envelope's
    top-level fields (``summary``, ``decision``, ``commit_sha``,
    ``artifacts``, ``metadata``) are universal; ``payload`` is the
    per-workflow polymorphic surface, validated by consumer convention
    per ADR-0015's per-workflow matrix.
    """

    ENTITY_TYPE: ClassVar[str] = "step"
    ACTION: ClassVar[str] = "completed"

    completed_at: datetime
    output: StepOutput
    token_usage: StepTokenUsage | None = None
    """Per-step LLM token usage telemetry (ADR-0020, Wave 1). Distinct
    from ``output.metadata`` because token usage is step-execution
    telemetry, not part of the polymorphic role/workflow output
    envelope. The consumer writes the five counters + ``model`` into
    the dedicated ``workflow_run_steps`` columns; absent ⇒ the columns
    stay NULL (validation steps, dry-run, or workers that bypass the
    LLM)."""


class StepFailed(EventPayload):
    """A worker failed to complete the step. ``error`` carries the
    short-form failure message; full diagnostics live in worker logs."""

    ENTITY_TYPE: ClassVar[str] = "step"
    ACTION: ClassVar[str] = "failed"

    failed_at: datetime
    error: str


class StepCancelled(EventPayload):
    """A step was cancelled — typically because a sibling step in the
    same run failed or because the parent task was cancelled."""

    ENTITY_TYPE: ClassVar[str] = "step"
    ACTION: ClassVar[str] = "cancelled"

    reason: str | None = None
