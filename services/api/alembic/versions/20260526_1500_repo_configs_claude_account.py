"""Add repo_configs.claude_account for per-account Claude credential routing (ADR-0055).

Workers resolve the Claude credential at the per-step seam by reading the
repo's ``claude_account`` (or the deployment's ``claude_default_account``
when ``NULL``) and fetching the matching secret from Secrets Manager. The
column is nullable so existing onboarded repos continue to use the default
account without any backfill.

Revision ID: 20260526_1500
Revises: 20260522_1200
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260526_1500"
down_revision: Union[str, Sequence[str], None] = "20260522_1200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repo_configs",
        sa.Column("claude_account", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("repo_configs", "claude_account")
