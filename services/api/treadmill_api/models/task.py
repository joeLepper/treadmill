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
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
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
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """Snapshot-on-submission per ADR-0010: tasks pin a specific workflow
    version row, so editing the workflow does not retroactively change the
    task's behavior."""

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        Index("ix_tasks_plan_id", "plan_id"),
        Index("ix_tasks_repo", "repo"),
        Index("ix_tasks_workflow_version_id", "workflow_version_id"),
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


class TaskValidation(Base):
    """An ordered validation check attached to a task — the ``validation:``
    block from a plan-doc task spec (per the 2026-05-11 closure plan D.3 +
    decision #14).

    Two kinds at v0:

      * ``deterministic`` — an executable check (e.g. ``pytest tests/...``)
        that the validator workflow runs against the task's PR.
      * ``llm-judge``     — a natural-language criterion the validator
        workflow evaluates via an LLM judge.

    The kinds are gated by a ``CHECK`` constraint so an unknown ``kind``
    cannot enter the table. ``position`` enforces a deterministic ordering
    so re-renders of a plan doc preserve check identity.
    """

    __tablename__ = "task_validations"

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
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('deterministic', 'llm-judge')",
            name="ck_task_validations_kind",
        ),
        UniqueConstraint(
            "task_id", "position", name="uq_task_validations_task_position"
        ),
        Index("ix_task_validations_task_id", "task_id"),
    )
