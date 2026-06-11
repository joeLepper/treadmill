"""``llm_calls`` + ``llm_harvest_cursors`` ORM models — token attribution.

One ``llm_calls`` row per LLM API call. Two writers:

* ``POST /api/v1/llm_calls`` — worker (via coordinator relay) records a
  subprocess invocation against a known ``task_execution_id``.
* ``POST /api/v1/llm_calls/harvest`` — the ADR-0089 transcript
  harvester (``treadmill tokens harvest``) bulk-inserts calls parsed
  from session transcript JSONL.

DECISION (ADR-0089 §2 left nullable-vs-synthetic-row to the
implementer): ``task_execution_id`` is **nullable**. Orchestrator and
coordinator calls have no dispatch cycle, so they have no execution
row; synthesizing one would require a fake ``tasks`` parent and would
leak into the ``task_status`` VIEW. Harvested rows always carry
``session_label`` (the report's GROUP BY key) and set
``task_execution_id`` only when exactly one execution window
(worker_label + started_at..completed_at) matches ``called_at``.

Per-plan burn remains the JOIN chain:
    tasks → task_executions → llm_calls → SUM(input_tokens + output_tokens)

``llm_harvest_cursors`` is the harvester's idempotency cursor: one row
per transcript file, recording the byte offset consumed so far and the
cumulative count of unparseable lines (ADR-0089: counted and reported,
never silently skipped). The (transcript_path, request_id) partial
UNIQUE index on ``llm_calls`` backstops the cursor against a response
that was still streaming when a harvest run snapshotted the file.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        Index(
            "uq_llm_calls_transcript_request",
            "transcript_path",
            "request_id",
            unique=True,
            postgresql_where=text(
                "transcript_path IS NOT NULL AND request_id IS NOT NULL"
            ),
        ),
        Index(
            "ix_llm_calls_session_label_called_at",
            "session_label",
            "called_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_executions.id", ondelete="CASCADE"),
        nullable=True,
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    cache_creation_tokens: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    model: Mapped[str] = mapped_column(Text(), nullable=False)
    # ── Harvester provenance (NULL on POST /llm_calls rows) ────────────
    session_label: Mapped[str | None] = mapped_column(Text(), nullable=True)
    called_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    transcript_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )


class LLMHarvestCursor(Base):
    __tablename__ = "llm_harvest_cursors"

    transcript_path: Mapped[str] = mapped_column(Text(), primary_key=True)
    byte_offset: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    malformed_lines: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
