"""tasks + task_executions: add ``generation`` (ADR-0118 phase 1, SC3 idempotency).

The coordinator-router dispatches each task's AUTHOR at most once per rework cycle, and
re-dispatches on a genuine new cycle. ADR-0118 SC3 keys that on ``(task_id, generation)``:
a cycle counter that starts at 1 and is bumped on each rework. There was no schema source
for it (``task_executions.trigger`` counts rework but is not a cycle key), so a re-delivered
event and a real rework were indistinguishable to a would-be deterministic dispatcher.

We add:
  - ``tasks.generation`` — the CURRENT cycle (source of truth; the router bumps it on a
    rework verdict before re-dispatching the author).
  - ``task_executions.generation`` — the cycle a given dispatch SERVED (immutable record).
  - a PARTIAL UNIQUE index ``(task_id, generation)`` over AUTHOR-dispatch triggers only, so
    the database itself makes an author re-dispatch for a cycle a no-op (idempotency by
    construction, not by agent prose). Peer-review rows are outside the index — a cycle may
    hold one author dispatch plus peer-review dispatches.

Backfill ranks existing author rows per task by ``started_at`` so pre-existing multi-cycle
tasks get distinct generations (else the unique index would fail on the all-default-1 rows).

Revision ID: 20260915_0100
Revises: 20260914_0100
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260915_0100"
down_revision: Union[str, Sequence[str], None] = "20260914_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AUTHOR_TRIGGERS = "('initial', 'coordinator-rework', 'evaluator-rework')"


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "task_executions",
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
    )

    # Backfill: give each existing AUTHOR dispatch a distinct per-task generation by
    # started_at order, so the partial unique index can be built on real historical data.
    op.execute(
        f"""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY started_at, id) AS rn
            FROM task_executions
            WHERE trigger IN {_AUTHOR_TRIGGERS}
        )
        UPDATE task_executions te
        SET generation = ranked.rn
        FROM ranked
        WHERE te.id = ranked.id
        """
    )
    # tasks.generation = the highest author-generation the task reached (default 1).
    op.execute(
        f"""
        UPDATE tasks t
        SET generation = COALESCE(
            (SELECT MAX(te.generation) FROM task_executions te
             WHERE te.task_id = t.id AND te.trigger IN {_AUTHOR_TRIGGERS}),
            1)
        """
    )

    # Idempotency by construction: at most one AUTHOR dispatch per (task_id, generation).
    op.create_index(
        "uq_task_executions_author_generation",
        "task_executions",
        ["task_id", "generation"],
        unique=True,
        postgresql_where=sa.text(f"trigger IN {_AUTHOR_TRIGGERS}"),
    )


def downgrade() -> None:
    op.drop_index("uq_task_executions_author_generation", table_name="task_executions")
    op.drop_column("task_executions", "generation")
    op.drop_column("tasks", "generation")
