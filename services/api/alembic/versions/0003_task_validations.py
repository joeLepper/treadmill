"""task_validations table — the ``validation:`` block side-table per
decision #14 in the 2026-05-11 Week-2 closure plan.

A plan-doc task spec may declare an ordered list of validation checks:

    - id: t0
      title: Add /health endpoint
      validation:
        - kind: deterministic
          description: pytest services/api/tests/test_health.py
        - kind: llm-judge
          description: the endpoint returns under 50ms p95

Each row is a single check. ``position`` defines render-stable ordering.
``kind`` is gated by a ``CHECK`` constraint at v0; future kinds expand
the constraint rather than removing it.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_validations",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('deterministic', 'llm-judge')",
            name="ck_task_validations_kind",
        ),
        sa.UniqueConstraint(
            "task_id", "position", name="uq_task_validations_task_position",
        ),
    )
    op.create_index(
        "ix_task_validations_task_id", "task_validations", ["task_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_validations_task_id", table_name="task_validations")
    op.drop_table("task_validations")
