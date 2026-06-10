"""Event audit log per ADR-0011.

Events are immutable. Status is derived from this log via the
``task_status`` VIEW (Day 2C). The consumer pattern (single writer)
applies status updates to ``workflow_run_steps.status`` based on these
events; nothing else writes status anywhere.

The ``payload`` JSONB column is the only place in Treadmill where JSONB
is explicitly allowed (per ADR-0011, plus ``workflow_run_steps.output``).
ALL reads/writes go through per-event-type Pydantic models in
``treadmill_api.events`` (Day 2B authors them); raw ``dict[str, Any]``
access of the column is forbidden by reviewer convention.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    """``task`` | ``plan`` | ``step`` | ``workflow_run`` | ``github`` |
    ``deployment`` | ``validation`` (for future ADR-0007 events)."""

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    """The verb. ``registered``, ``ready``, ``started``, ``completed``,
    ``failed``, ``cancelled``, ``pr_opened``, ``pr_merged``, etc."""

    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    """Historical reference to a pre-ADR-0087 workflow_run. The FK was
    dropped with the table (Phase 4); the column survives as a plain
    UUID so the events audit trail keeps its lineage. The ORM-level
    ForeignKey had to go too — SQLAlchemy resolves FK targets against
    Base.metadata at flush time and raises NoReferencedTableError once
    the referenced model is gone (first surfaced as a 500 on every
    events INSERT after the Phase 5 deploy)."""
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    """Historical reference to a pre-ADR-0087 workflow_run_step — same
    plain-UUID treatment as ``run_id`` above."""
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The HEAD this event ran against (or describes), per ADR-0014.

    Populated by the webhook receiver for ``github.*`` events (from the
    GitHub payload's commit-SHA field — ``pr.head.sha``,
    ``check_run.head_sha``, ``review.commit_id``, etc.); populated by
    the coordination consumer for ``step.completed`` from the envelope's
    top-level ``commit_sha``; populated by the dispatcher when it stamps
    a ``step.ready`` against a task whose PR's HEAD has been resolved.
    NULL for pre-commit events (e.g. ``plan.registered``).

    ADR-0013's ``task_mergeability`` VIEW joins on this column; partial
    indexes accelerate the join without bloating over the NULL majority.
    """
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        Index("ix_events_entity_action", "entity_type", "action"),
        Index("ix_events_task_id", "task_id"),
        Index("ix_events_plan_id", "plan_id"),
        Index("ix_events_run_id", "run_id"),
        Index("ix_events_created_at", "created_at"),
        # Partial indexes per ADR-0014 — accelerate the mergeability
        # VIEW's joins on ``commit_sha`` without bloating over the NULL
        # majority of pre-commit events.
        Index(
            "ix_events_task_commit",
            "task_id",
            "commit_sha",
            postgresql_where=text("commit_sha IS NOT NULL"),
        ),
        Index(
            "ix_events_entity_action_commit",
            "entity_type",
            "action",
            "commit_sha",
            postgresql_where=text("commit_sha IS NOT NULL"),
        ),
    )
