"""Workflow run + step entities per ADR-0010 + ADR-0011.

A WorkflowRun is one execution of a workflow against a task. Runs are
append-only — a task may have many runs over its lifetime as automation
fires (wf-author → wf-review → wf-feedback → wf-ci-fix → ...).

WorkflowRunStep status is the single mutable column per ADR-0011's
"single-writer projection" pattern: only the event consumer updates it
when ``step.completed`` / ``step.failed`` events arrive. The tasks +
plans tables have no equivalent column — their status is derived via
the ``task_status`` VIEW (Day 2C).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    """How this run started: ``registered`` (initial dispatch),
    ``webhook:pr_opened``, ``webhook:check_run_completed``, etc."""

    source_step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_run_steps.id", ondelete="SET NULL"),
        nullable=True,
    )
    """FK to the ``workflow_run_steps`` row whose completion triggered
    this run, when this run was dispatched as a self-trigger side-effect
    of an upstream step. ``None`` for the common paths (initial
    dispatch, webhook fan-out, deadlock arbitration) — only set on the
    self-trigger paths that need to plumb the upstream step's output
    into the downstream worker's context.

    Today only ``maybe_dispatch_feedback_on_architect_amend`` populates
    it (with the ``wf-architecture-resolve`` step that emitted
    ``amend``) so the downstream ``role-feedback-analyzer`` can read the
    architect's ``remediation_summary`` + ``reasoning`` from
    ``workflow_run_steps.output`` and honor the directive verbatim. The
    steps router joins through this FK and exposes the source step on
    the ``WorkerContextResponse`` as a ``source_step`` block so the
    worker sees it on its initial step-context fetch.

    Per ADR-0011: ``workflow_run_steps.output`` is already JSONB (one of
    the two sites the architecture commits to JSONB); the structured-FK
    shape here keeps the lineage typed without adding a new JSONB
    column on ``workflow_runs``."""

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        Index("ix_workflow_runs_task_id", "task_id"),
        Index("ix_workflow_runs_workflow_version_id", "workflow_version_id"),
    )


class WorkflowRunStep(Base):
    """One step within a run. ``status`` is the single mutable column,
    written only by the event consumer when step lifecycle events arrive
    (per ADR-0011's single-writer projection pattern).

    Status values: ``pending`` → ``running`` → (``completed`` | ``failed``
    | ``cancelled``).
    """

    __tablename__ = "workflow_run_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("roles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'pending'"),
    )
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    """Polymorphic step output (PR URL + branch, validation summary, etc.).
    ALL access goes through Pydantic step-output models per ADR-0011 — never
    raw ``dict`` lookups. Day 2B authors the per-step-type models."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id", "step_index",
            name="uq_workflow_run_steps_run_index",
        ),
        Index("ix_workflow_run_steps_run_id", "run_id"),
        Index("ix_workflow_run_steps_status", "status"),
    )
