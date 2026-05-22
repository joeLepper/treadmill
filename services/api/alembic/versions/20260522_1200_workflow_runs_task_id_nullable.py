"""Make workflow_runs.task_id nullable for taskless scheduled dispatch (ADR-0035).

Scheduled (taskless) workflow dispatch was silently broken: the scheduler
ticked but no scheduled ``workflow_run`` ever persisted, because
``workflow_runs.task_id`` was ``NOT NULL`` and the scheduled path
(``_create_and_publish_run_without_task`` in ``coordination/triggers.py``)
creates a run with ``task_id=None``. Schedules sweep an entire repo, not a
specific PR — they have no inherent task context.

This migration relaxes the NOT NULL constraint on ``workflow_runs.task_id``.
The existing FK to ``tasks.id`` is preserved (a nullable FK is valid). Task-
bound runs (the common case) continue to populate it; schedule-triggered
runs leave it ``NULL``.

Revision ID: 20260522_1200
Revises: 20260521_1857
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260522_1200"
down_revision: Union[str, Sequence[str], None] = "20260521_1857"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "workflow_runs",
        "task_id",
        existing_type=sa.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    # Note: this will fail if any taskless (schedule-triggered) rows exist —
    # acceptable, since by the time we'd downgrade, the scheduled-dispatch
    # path would need to be reverted in code first.
    op.alter_column(
        "workflow_runs",
        "task_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
