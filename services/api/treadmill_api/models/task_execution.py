"""``task_executions`` ORM model — ADR-0087 per-dispatch-cycle record.

One row per worker dispatch. The coordinator writes a row at dispatch
time and patches ``status`` + ``completed_at`` (or ``failure_reason``)
on resolution. The trigger column records why the worker was dispatched
so rework cycles and peer-review passes are independently queryable.

Rework count per task:
    SELECT COUNT(*) FROM task_executions
    WHERE task_id = :id
      AND trigger IN ('coordinator-rework', 'evaluator-rework')
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base

_VALID_TRIGGERS = ("initial", "coordinator-rework", "evaluator-rework", "peer-review")
_VALID_STATUSES = ("running", "completed", "failed")


class TaskExecution(Base):
    __tablename__ = "task_executions"
    __table_args__ = (
        CheckConstraint(
            f"trigger IN {_VALID_TRIGGERS!r}",
            name="ck_task_executions_trigger",
        ),
        CheckConstraint(
            f"status IN {_VALID_STATUSES!r}",
            name="ck_task_executions_status",
        ),
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
    worker_label: Mapped[str] = mapped_column(Text(), nullable=False)
    trigger: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'running'"),
    )
    failure_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
