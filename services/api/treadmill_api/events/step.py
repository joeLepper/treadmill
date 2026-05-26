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


# ── Step-execution telemetry sub-models ───────────────────────────────────────


class StepTokenUsage(BaseModel):
    """Per-step LLM token counters + model attribution (ADR-0020).

    Carried on ``StepCompleted`` as a distinct optional field — *not*
    folded into ``StepOutput.metadata`` — because token usage is
    step-execution telemetry that the consumer projects onto dedicated
    columns on ``workflow_run_steps``, not workflow-content metadata
    that belongs in the uniform envelope. Worker shape (claude_code's
    ``CodeAuthorResult.token_usage`` + ``model``) mirrors this exactly.

    All sub-fields are required when ``StepCompleted.token_usage`` is
    present; the sub-model itself is optional on the parent (steps
    that made no LLM call — dry-run, ``wf-validate`` — omit it)."""

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
    """Per-step LLM token counters + model. ``None`` when the step made
    no LLM call (dry-run, ``wf-validate``). The consumer projects this
    onto five nullable columns on ``workflow_run_steps`` in the same
    UPDATE that writes ``status='completed'`` (ADR-0020)."""


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
