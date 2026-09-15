"""``evaluator_dispatches`` ORM model — ADR-0118 phase 1, the re-eval dedup guard.

One row per evaluator dispatch, UNIQUE(task_id, head_sha): the first ci_result that finds the
required CI checks terminal+passing for a head fires the evaluator and inserts a row; every
later ci_result for the same head no-ops on the constraint (a trailing non-required suite must
not re-invoke the evaluator). A genuine new head (a rework push) is a new head_sha → a new
evaluation. This is the eval-path analog of the ``(task_id, generation)`` author-dispatch index.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class EvaluatorDispatch(Base):
    __tablename__ = "evaluator_dispatches"
    __table_args__ = (
        UniqueConstraint("task_id", "head_sha", name="uq_evaluator_dispatches_task_head"),
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
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
