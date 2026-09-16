"""verdict_applications — apply-a-verdict-once guard (ADR-0118 verdict loop).

The router applies an evaluator verdict exactly once per (task, head): ``rework`` bumps the
task generation + re-dispatches the author; ``approve`` records the approval for integration.
Idempotency must be BY CONSTRUCTION (not SELECT-then-act under a single-consumer assumption),
matching the ``(task_id, generation)`` author index and ``evaluator_dispatches`` — so the apply
INSERTs a row here first and a second apply for the same (task, head) hits the unique
constraint and no-ops. A rework produces a new head_sha, so the next verdict claims a new row.

Revision ID: 20260916_0100
Revises: 20260915_0300
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260916_0100"
down_revision: Union[str, Sequence[str], None] = "20260915_0300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "verdict_applications",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "task_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id"),
            nullable=False,
        ),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("applied_generation", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("task_id", "head_sha", name="uq_verdict_applications_task_head"),
    )


def downgrade() -> None:
    op.drop_table("verdict_applications")
