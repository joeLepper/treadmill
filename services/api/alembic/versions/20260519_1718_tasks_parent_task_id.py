"""Add tasks.parent_task_id self-FK for supersede lineage (ADR-0048).

ADR-0048 reframes the architect's ``supersede`` verdict: when the
architect decides the plan-text itself was wrong (not just the code),
the supersede trigger closes the existing PR, creates a NEW task row
with a rewritten description, and dispatches a fresh ``wf-author``
against the child. Task text remains immutable per row — supersede is
a new row, not an in-place edit. This migration adds the
``parent_task_id`` self-FK that links child → parent.

Columns:

  * ``parent_task_id UUID NULL`` — FK to ``tasks.id`` ON DELETE SET NULL.
    SET NULL (not CASCADE) so an administrative delete of the parent
    doesn't cascade-delete child work; the lineage breaks gracefully.

Indexes:

  * ``ix_tasks_parent_task_id`` — backs ``WHERE parent_task_id = ?``
    lookups (e.g. "find all children of this task" for operator UI).

Revision ID: 20260519_1718
Revises: 20260518_1715
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260519_1718"
down_revision: Union[str, Sequence[str], None] = "20260518_1715"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "parent_task_id",
            sa.UUID(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_tasks_parent_task_id_tasks",
        "tasks",
        "tasks",
        ["parent_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_tasks_parent_task_id",
        "tasks",
        ["parent_task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_constraint(
        "fk_tasks_parent_task_id_tasks", "tasks", type_="foreignkey",
    )
    op.drop_column("tasks", "parent_task_id")
