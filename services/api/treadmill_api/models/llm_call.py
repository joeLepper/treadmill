"""``llm_calls`` ORM model — per-subprocess token attribution.

One row per Claude Code subprocess invocation, FK'd to
``task_executions`` ON DELETE CASCADE. A single task_execution may have
multiple llm_calls (e.g. initial write + CI-failure mid-task rework).

Per-plan burn is the JOIN chain:
    tasks → task_executions → llm_calls → SUM(input_tokens + output_tokens)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    cache_creation_tokens: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    model: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
