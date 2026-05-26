"""Add per-step token usage columns to workflow_run_steps (ADR-0020).

Persists the per-step Claude token counters (already parsed by the worker
from Claude Code's JSON envelope and emitted as OTel metrics) onto
``workflow_run_steps`` so the API has a durable, queryable record. The
worker now ships them on ``step.completed`` as the optional
``token_usage`` field; the coordination consumer projects each
sub-field onto its dedicated column in the same UPDATE that writes
``status='completed'``. All five columns are nullable so steps that made
no LLM call (dry-run, wf-validate) — and historical rows — remain valid.

Revision ID: 20260526_1600
Revises: 20260526_1500
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260526_1600"
down_revision: Union[str, Sequence[str], None] = "20260526_1500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflow_run_steps",
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "workflow_run_steps",
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "workflow_run_steps",
        sa.Column("cache_creation_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "workflow_run_steps",
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "workflow_run_steps",
        sa.Column("model", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflow_run_steps", "model")
    op.drop_column("workflow_run_steps", "cache_read_tokens")
    op.drop_column("workflow_run_steps", "cache_creation_tokens")
    op.drop_column("workflow_run_steps", "output_tokens")
    op.drop_column("workflow_run_steps", "input_tokens")
