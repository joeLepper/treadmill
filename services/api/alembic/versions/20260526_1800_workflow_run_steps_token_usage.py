"""Add per-step token usage columns to workflow_run_steps (ADR-0020 Wave 1).

Token usage is parsed from the Claude Code JSON envelope, ridden up on
``step.completed.token_usage`` (a typed sub-model distinct from the
polymorphic ``StepOutput.metadata``), and persisted by the coordination
consumer into these dedicated columns. All five are nullable: validation
steps, dry-run paths, and historical rows from before Wave 1 leave them
NULL — only LLM-driven step.completed events populate them.

Revision ID: 20260526_1800
Revises: 20260526_1500
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260526_1800"
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
