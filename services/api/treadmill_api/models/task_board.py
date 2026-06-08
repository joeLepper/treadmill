"""Task board — coordinator's team-coordination overlay on top of the
event-derived ``task_status`` view (Task 1C / Phase 1 of ADR-0084).

Per ADR-0084 §6, the task board is a coordinator-maintained planning
overlay derived from and cross-checked against the DB. The ``tasks`` /
``workflow_runs`` tables remain the source of truth for plan/task
lifecycle; the task board's ``status`` is a coordinator-written overlay
that captures team-coordination state (who is working on what, what is
ready to be picked up, what is blocked and why).

The two vocabularies are adjacent, not 1:1: the event-derived
``task_status`` view exposes lifecycle states (``registered``,
``{workflow_id}: executing``, ``pr_merged``, etc.) while ``task_board``
uses coordinator-semantics (``ready``, ``in_flight``, ``waiting_ci``,
``waiting_review``, ``done``, ``blocked_dependency``, ``blocked_operator``,
``superseded``, ``cancelled``, ``failed``).

Schema invariants
-----------------
- ``task_id`` PK + CASCADE delete from ``tasks``: when a task is deleted
  (administrative cleanup) the board entry follows.
- ``plan_id`` FK + CASCADE: ditto for plan deletion.
- ``status`` is required and validated at the application layer against
  the vocabulary above (no DB-level CHECK constraint to avoid coupling
  schema changes to vocab evolution).
- ``updated_at`` defaults to ``now()`` at insert; the PATCH endpoint
  refreshes it on every write.
- ``updated_by`` records the writer's label (e.g. ``coordinator-medicoder``)
  for audit; nullable because the initial reconciliation INSERT may be
  triggered by a startup task with no specific operator label.

ADR-0011 invariant preservation
-------------------------------
The ``tasks`` table has no mutable status column. ADR-0084 §6 explicitly
calls out that adding columns to ``tasks`` would break the append-only
invariant the schema rests on. The task_board is a separate table; the
event-derived view stays authoritative for lifecycle state. The
coordinator reconciles on startup and on each significant event.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


# Status vocabulary, exposed for application-layer validation. Kept here
# so the model file is the single source of truth — routers + tests
# import from this list rather than redeclaring.
TASK_BOARD_STATUSES: frozenset[str] = frozenset(
    {
        "ready",
        "in_flight",
        "waiting_ci",
        "waiting_review",
        "done",
        "blocked_dependency",
        "blocked_operator",
        "superseded",
        "cancelled",
        "failed",
    }
)


class TaskBoard(Base):
    """One row per task on the coordinator's overlay board."""

    __tablename__ = "task_board"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    """Denormalized from ``tasks.plan_id`` so the GET-by-plan_id endpoint
    can index-scan this column directly. The application layer keeps the
    two in sync on the rare task-reparent edge case."""

    assignee: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    """Worker label (e.g. ``treadmill-bert``). NULL when a task is
    ``ready`` and not yet picked up, or when an assignee just released
    the task."""

    status: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    """One of ``TASK_BOARD_STATUSES``. Application-layer validated;
    intentionally not a DB CHECK so vocab evolution doesn't require a
    migration on every addition."""

    branch: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    pr_number: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    """Free-form coordinator notes — pitfalls discovered, blocking
    context, hand-off notes. Surfaced in the coordinator briefing as
    context for the next worker on the task."""

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    updated_by: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    """Coordinator label that performed the most recent write. NULL when
    the row was created by an unattributed reconciliation pass."""

    __table_args__ = (
        Index("ix_task_board_plan_id", "plan_id"),
        Index("ix_task_board_assignee", "assignee"),
        Index("ix_task_board_status", "status"),
    )
