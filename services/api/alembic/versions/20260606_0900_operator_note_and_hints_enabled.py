"""Add operator_note to tasks and worker_hints_enabled to repo_configs.

Per ADR-0081 §1: operator_note is a nullable text field on tasks that
workers read via the per-step context fetch and inject into the system
prompt. worker_hints_enabled gates the feature per-repo (defaults true).

Revision ID: 20260606_0900
Revises: 20260605_1900
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260606_0900"
down_revision: Union[str, Sequence[str], None] = "20260605_1900"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("operator_note", sa.Text(), nullable=True),
    )
    op.add_column(
        "repo_configs",
        sa.Column(
            "worker_hints_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_configs", "worker_hints_enabled")
    op.drop_column("tasks", "operator_note")
