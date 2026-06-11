"""Harvested ``llm_calls`` — ADR-0089 §2 meter wiring.

The ``llm_calls`` table was created by ``20260610_1000`` with a NOT NULL
FK to ``task_executions`` and zero writers. ADR-0089's harvester walks
session transcript JSONL and inserts one row per LLM call, including
calls made by orchestrator/coordinator sessions that have **no**
``task_executions`` row.

DECISION (ADR-0089 left it to the implementer): ``task_execution_id``
is **relaxed to nullable** rather than synthesizing a per-session
execution row. Synthetic rows would need a synthetic ``tasks`` FK
parent (``task_executions.task_id`` is NOT NULL), would inherit the
trigger/status CHECK constraints that encode dispatch semantics they
don't have, and would leak into the ``task_status`` VIEW (clause 3a
derives ``executing`` from a running execution). A NULL FK is the
honest shape: the call simply has no dispatch cycle. Per-label
attribution for such calls rides the new ``session_label`` column.

New ``llm_calls`` columns (all nullable — pre-harvest rows written via
``POST /api/v1/llm_calls`` don't have them):

* ``session_label``    — label attributed from the transcript's project
                         dir (e.g. ``worker-joelepper-treadmill-2``);
                         the report's GROUP BY key.
* ``called_at``        — the call's transcript timestamp (``created_at``
                         remains row-insert time).
* ``transcript_path`` / ``request_id`` — provenance; their partial
  UNIQUE index is the idempotency backstop: a response that was still
  streaming when a harvest run snapshotted the file straddles the byte
  cursor, and the cursor alone would re-insert its requestId on the
  next run. The harvest endpoint resolves the conflict with
  ``ON CONFLICT DO UPDATE`` (last-write-wins): the colliding row was
  recorded from a mid-stream line with undercounted usage, and the
  re-send carries the completed response's true totals.

New table ``llm_harvest_cursors`` — the primary idempotency mechanism:
one row per transcript file recording how far harvesting has consumed
it (``byte_offset``) and how many unparseable lines it has counted
(``malformed_lines``, cumulative — ADR-0089 requires these COUNTED AND
REPORTED, never silently skipped).

Revision ID: 20260611_0600
Revises: 20260611_0500
Create Date: 2026-06-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260611_0600"
down_revision: Union[str, None] = "20260611_0500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("llm_calls", "task_execution_id", nullable=True)
    op.add_column("llm_calls", sa.Column("session_label", sa.Text(), nullable=True))
    op.add_column(
        "llm_calls",
        sa.Column("called_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("llm_calls", sa.Column("transcript_path", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("request_id", sa.Text(), nullable=True))

    op.create_index(
        "uq_llm_calls_transcript_request",
        "llm_calls",
        ["transcript_path", "request_id"],
        unique=True,
        postgresql_where=sa.text(
            "transcript_path IS NOT NULL AND request_id IS NOT NULL"
        ),
    )
    # The report's WHERE called_at >= :since … GROUP BY session_label.
    op.create_index(
        "ix_llm_calls_session_label_called_at",
        "llm_calls",
        ["session_label", "called_at"],
    )

    op.create_table(
        "llm_harvest_cursors",
        sa.Column("transcript_path", sa.Text(), primary_key=True),
        sa.Column("byte_offset", sa.BigInteger(), nullable=False),
        sa.Column(
            "malformed_lines",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_harvest_cursors")
    op.drop_index("ix_llm_calls_session_label_called_at", table_name="llm_calls")
    op.drop_index("uq_llm_calls_transcript_request", table_name="llm_calls")
    op.drop_column("llm_calls", "request_id")
    op.drop_column("llm_calls", "transcript_path")
    op.drop_column("llm_calls", "called_at")
    op.drop_column("llm_calls", "session_label")
    # Harvested orchestrator rows have no execution; they cannot survive
    # the NOT NULL restore.
    op.execute("DELETE FROM llm_calls WHERE task_execution_id IS NULL")
    op.alter_column("llm_calls", "task_execution_id", nullable=False)
