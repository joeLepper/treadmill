"""Add repo_configs.claude_account_fallback for usage-limit fallback routing (ADR-0066).

When the primary ``claude_account`` hits a usage limit the worker can fall back
to this account instead of failing the step. The column is nullable so existing
onboarded repos continue to work without a backfill — ``NULL`` means no fallback
is configured.

Revision ID: 20260604_0100
Revises: 20260528_1400
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260604_0100"
down_revision: Union[str, Sequence[str], None] = "20260528_1400"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repo_configs",
        sa.Column("claude_account_fallback", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("repo_configs", "claude_account_fallback")
