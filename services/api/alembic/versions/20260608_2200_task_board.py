"""Add task_board table — coordinator overlay (ADR-0084 §6 / Task 1C).

The task_board is a coordinator-maintained planning overlay on top of the
event-derived task_status view. Per ADR-0084 it is a separate table (not
columns added to ``tasks``) so the append-only invariant the ``tasks``
schema rests on stays intact.

Three indexes: plan_id (for GET-by-plan_id), assignee (for
who-is-working-on-what queries), status (for board-shaped reads).

No CHECK constraint on ``status`` — vocab is validated at the application
layer so vocab evolution doesn't force a migration on every addition.

Revision ID: 20260608_2200
Revises: 20260606_0900
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID


revision: str = "20260608_2200"
down_revision: Union[str, Sequence[str], None] = "20260606_0900"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_board",
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "plan_id",
            UUID(as_uuid=True),
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assignee", sa.String(255), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("pr_number", sa.Integer, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "updated_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by", sa.String(255), nullable=True),
    )

    op.create_index("ix_task_board_plan_id", "task_board", ["plan_id"])
    op.create_index("ix_task_board_assignee", "task_board", ["assignee"])
    op.create_index("ix_task_board_status", "task_board", ["status"])


def downgrade() -> None:
    op.drop_index("ix_task_board_status", table_name="task_board")
    op.drop_index("ix_task_board_assignee", table_name="task_board")
    op.drop_index("ix_task_board_plan_id", table_name="task_board")
    op.drop_table("task_board")
