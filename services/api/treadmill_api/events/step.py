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

from treadmill_api.events.base import EventPayload
from treadmill_api.events.step_output import StepOutput


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
