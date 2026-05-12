"""workflow_dispatch_dedup table per ADR-0026.

ADR-0026 introduces a per-dispatch dedup table to gate the trigger
evaluator's ``dispatch_task`` calls. A single PR can fire repeated
``pull_request_review`` / ``pull_request_synchronize`` webhooks; without
dedup, ``wf-review`` and ``wf-feedback`` runs accumulate on identical
content (PR #10 today: 3+ wf-review runs against the same head SHA).

The table is a tiny gate: one PK on ``dedup_key`` (text), one
``workflow_run_id`` (uuid, no FK — see below), one ``dispatched_at``
timestamp. The PK collision *is* the dedup mechanism; the insert-first
ordering means we never even create a workflow_run row that would have
to be rolled back.

Per ADR-0026 §"Optimistic pre-check + PK gate ordering" the
``workflow_run_id`` column intentionally does NOT carry a FK to
``workflow_runs(id)``. The insert-first/dispatch-second flow needs to
write a row with a sentinel ``workflow_run_id`` before the run exists;
adding a FK would either require deferring the constraint or making the
column nullable. v0 picks "relaxed FK" so the constraint pattern is the
simplest possible: PK on text, no foreign keys, no indexes beyond the PK.

Operator-visibility query:

  SELECT * FROM workflow_dispatch_dedup
  WHERE dedup_key LIKE 'wf-review:%'
  ORDER BY dispatched_at DESC;

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_dispatch_dedup",
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column(
            "workflow_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "dispatched_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "dedup_key", name="pk_workflow_dispatch_dedup",
        ),
    )


def downgrade() -> None:
    op.drop_table("workflow_dispatch_dedup")
