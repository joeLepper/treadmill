"""``verdict_applications`` ORM model — ADR-0118 verdict loop, the apply-once guard.

The evaluator posts a ``task.evaluator_verdict`` event; the router's dispatch consumer applies
it (``rework`` → generation bump + author re-dispatch; ``approve`` → recorded for integration).
The apply must be EXACTLY-ONCE: a re-delivered or double-POSTed verdict — or, under a
multi-replica deployment, a concurrent delivery — must not bump the generation twice or
re-dispatch the author twice.

This table is that guard, keyed UNIQUE(task_id, head_sha): the consumer INSERTs a row as the
FIRST step of applying a verdict; a second apply for the same (task, head) hits the constraint
and no-ops at the INSERT. That makes the verdict path idempotent BY CONSTRUCTION — matching the
``(task_id, generation)`` author-dispatch index and ``evaluator_dispatches`` UNIQUE(task_id,
head_sha), rather than resting on a SELECT-then-act + single-consumer assumption (Bert review,
PR #416). A genuine rework produces a NEW head_sha, so the next verdict claims a new row.

``decision`` + ``applied_generation`` are recorded so the future integrator has a durable,
complete work-list of approved (task, head) pairs, and so a rework's retired generation is
auditable.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class VerdictApplication(Base):
    __tablename__ = "verdict_applications"
    __table_args__ = (
        UniqueConstraint("task_id", "head_sha", name="uq_verdict_applications_task_head"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id"),
        nullable=False,
    )
    head_sha: Mapped[str] = mapped_column(Text(), nullable=False)
    decision: Mapped[str] = mapped_column(Text(), nullable=False)
    """``approve`` | ``rework`` — the verdict the router applied for this (task, head)."""
    applied_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    """The task generation the verdict was applied against. On ``rework`` this is the generation
    the router RETIRED — it bumped to ``applied_generation + 1`` and re-dispatched the author."""
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
