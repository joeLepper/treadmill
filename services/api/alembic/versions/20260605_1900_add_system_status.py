"""Create system_status table for autoscaler heartbeat + detector reads.

The ``system_status`` table holds autoscaler state (worker count, spawn history)
per family. Single row per family — mirrors Schedule/Hook shape. Timestamps are
timezone-aware. The detector subsystem (task 4) reads current state via the
GET endpoint; autoscaler writes via POST on each tick.

Revision ID: 20260605_1900
Revises: 20260605_1800
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260605_1900"
down_revision: Union[str, Sequence[str], None] = "20260605_1800"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_status",
        sa.Column("family", sa.String(64), primary_key=True),
        sa.Column("worker_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_spawn_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_spawn_error", sa.Text(), nullable=True),
        sa.Column("last_consume_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "consecutive_spawn_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_system_status_updated_at",
        "system_status",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_system_status_updated_at", table_name="system_status")
    op.drop_table("system_status")
