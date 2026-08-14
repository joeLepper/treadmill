"""Central event log table — ADR-0093.

Creates the ``event_log`` table that backs durable agent-to-agent messaging.
The outbox pump appends here; per-host consumers read from this table using
an ``offset > cursor`` query filtered to their bound labels.

``offset`` is BIGSERIAL (auto-incremented bigint) — the append position.
An index on ``ordering_key`` supports the per-consumer query:
``WHERE ordering_key IN (...) AND offset > :cursor ORDER BY offset ASC``.

Revision ID: 20260814_0100
Revises: 20260813_0100
Create Date: 2026-08-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260814_0100"
down_revision: Union[str, None] = "20260813_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_log",
        sa.Column(
            "offset",
            sa.BigInteger,
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("ordering_key", sa.String(255), nullable=False),
        sa.Column("dedup_key", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_event_log_ordering_key",
        "event_log",
        ["ordering_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_event_log_ordering_key", table_name="event_log")
    op.drop_table("event_log")
