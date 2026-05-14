"""plans: add auto_merge opt-out column (ADR-0031).

Per ADR-0031 Q31.c, per-plan opt-out of auto-merge is supported via the
plan's front-matter (``auto_merge: false``). This column stores the resolved
value: NULL or TRUE means auto-merge is enabled (the expected steady state),
FALSE means the plan has opted out.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("auto_merge", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plans", "auto_merge")
