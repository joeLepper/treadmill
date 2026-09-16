"""evaluator_dispatches — at-most-one re-eval per (task, head) (ADR-0118 phase 1, TRACE 2).

The ci_result→re-eval path (Bert's ``on_ci_result``) fires the evaluator when the required CI
checks for a head go terminal+passing. But ``is_ci_ready`` stays True once the required set is
terminal, so a TRAILING non-required suite's ``task.ci_result`` re-enters ``on_ci_result`` and
would re-invoke the evaluator. ``on_ci_result`` deliberately does NOT dedup — the WRITE dedups,
the eval-path analog of the ``(task_id, generation)`` author-dispatch index.

This table is that guard: one row per evaluator dispatch, UNIQUE(task_id, head_sha). The first
required-set-completing ci_result inserts a row (fires the evaluator); every later ci_result
for the same head hits the unique constraint → no-op. A genuine new head (a rework push) is a
new head_sha → a new row → a new evaluation, exactly as wanted.

Revision ID: 20260915_0300
Revises: 20260915_0200
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260915_0300"
down_revision: Union[str, Sequence[str], None] = "20260915_0200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evaluator_dispatches",
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
        sa.Column(
            "created_at",
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("task_id", "head_sha", name="uq_evaluator_dispatches_task_head"),
    )


def downgrade() -> None:
    op.drop_table("evaluator_dispatches")
