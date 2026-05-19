"""Add workflow_runs.source_step_id self-FK for cross-run dispatch lineage.

When a workflow run is dispatched as a side-effect of an upstream step
completing (e.g. ``wf-feedback`` fired by an architect's ``amend``
verdict on ``wf-architecture-resolve``), the downstream worker needs to
read the upstream step's output to honor its directive. Today the
upstream step.completed payload carries that directive (architect's
``remediation_summary`` + ``reasoning``) but it gets dropped at the
dispatch boundary — the downstream analyzer re-evaluates from scratch
and often re-concludes ``no code change needed`` against the architect's
explicit directive, blunting ADR-0048's architect-as-recoverer shape.

This migration adds a nullable UUID FK column pointing back at the
``workflow_run_steps`` row that triggered the dispatch. The steps router
joins through it to surface that step's ``output`` (already JSONB per
ADR-0011 — the one column Treadmill commits to JSONB) on the
``WorkerContextResponse`` so the downstream worker reads the architect's
verdict on its initial step.context fetch.

We deliberately do NOT add a new JSONB column: per ADR-0011,
``events.payload`` and ``workflow_run_steps.output`` are the only JSONB
sites in the schema. The structured-FK shape (column points at the row,
the existing JSONB column carries the payload) preserves that contract.

Columns:

  * ``source_step_id UUID NULL`` — FK to ``workflow_run_steps.id``
    ON DELETE SET NULL. SET NULL (not CASCADE) so administrative cleanup
    of an old step row doesn't cascade-delete unrelated downstream runs;
    the lineage breaks gracefully. ``NULL`` for the majority of runs
    (initial dispatch + webhook fan-out paths) — only the self-trigger
    paths that need to plumb upstream context populate it.

Indexes:

  * ``ix_workflow_runs_source_step_id`` — backs ``WHERE source_step_id =
    ?`` lookups (e.g. operator UI "show downstream runs triggered by
    this step").

Revision ID: 20260519_1930
Revises: 20260519_1718
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260519_1930"
down_revision: Union[str, Sequence[str], None] = "20260519_1718"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column(
            "source_step_id",
            sa.UUID(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_workflow_runs_source_step_id_workflow_run_steps",
        "workflow_runs",
        "workflow_run_steps",
        ["source_step_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_workflow_runs_source_step_id",
        "workflow_runs",
        ["source_step_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_runs_source_step_id", table_name="workflow_runs",
    )
    op.drop_constraint(
        "fk_workflow_runs_source_step_id_workflow_run_steps",
        "workflow_runs",
        type_="foreignkey",
    )
    op.drop_column("workflow_runs", "source_step_id")
