"""Task entities per ADR-0010 + ADR-0011.

Tasks are immutable records of intended code changes. Status is derived
from events, computed by the ``task_status`` VIEW (Day 2C, cribbed from
bunkhouse migration 020).

Four tables live in this module:
  - tasks             — the immutable task definitions
  - task_prs          — bridge from (repo, pr_number) → task_id (per ADR-0007)
  - task_dependencies — dependency expressions (e.g. ``task.t0.pr_merged``)
  - task_validations  — the ``validation:`` block from plan-doc task specs
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Per ADR-0081: operator-injected hint for the worker. Workers read this
    via the per-step context fetch and inject it into the system prompt when
    non-null and the repo's worker_hints_enabled is true. The operator sets this
    via POST /api/v1/tasks/{id}/operator_note or the CLI wrapper."""

    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Self-FK for ``supersede`` lineage (ADR-0048). When the architect
    verdicts ``supersede`` on a task whose plan needs a rewritten
    description, the supersede trigger creates a NEW task row with the
    rewritten ``description`` and ``parent_task_id`` pointing back to the
    original. The original task closes its PR and marks itself
    superseded; the child task is registered fresh and runs its own
    ``wf-author``. Task text remains immutable per row — supersede is a
    new row, not an in-place edit. ``ON DELETE SET NULL`` so an
    administrative delete of the parent doesn't cascade-delete child
    work (the lineage breaks gracefully, leaving the child standalone)."""

    __table_args__ = (
        Index("ix_tasks_plan_id", "plan_id"),
        Index("ix_tasks_repo", "repo"),
        Index("ix_tasks_parent_task_id", "parent_task_id"),
    )


class TaskPR(Base):
    """Bridge from (repo, pr_number) → task_id, per ADR-0007.

    Populated when a worker creates a PR. Webhook handlers query this first
    to find the task associated with a GitHub PR. Composite primary key
    matches bunkhouse's ``task_prs`` table shape exactly.
    """

    __tablename__ = "task_prs"

    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    branch: Mapped[str | None] = mapped_column(String(512), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The PR head commit SHA (ADR-0063-deferred lookup; ADR-0090's
    CI-observer resolves (repo, head_sha) → task through it). Nullable:
    legacy rows and writers that don't know the head yet leave it NULL —
    ``resolve_task_by_head_sha`` simply won't match those rows."""
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        PrimaryKeyConstraint("repo", "pr_number", name="pk_task_prs"),
        Index("ix_task_prs_task_id", "task_id"),
    )


class TaskDependency(Base):
    """A dependency expression attached to a task.

    Expression grammar (cribbed from bunkhouse, per ADR-0007):
      task.<id>.pr_merged
      task.<id>.run.completed
      task.<id>.step.<name>.completed
      task.<id>.event.<name>
      deployment.<env>.<repo>.validated
    """

    __tablename__ = "task_dependencies"

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
    expression: Mapped[str] = mapped_column(String(512), nullable=False)

    __table_args__ = (
        Index("ix_task_dependencies_task_id", "task_id"),
    )
