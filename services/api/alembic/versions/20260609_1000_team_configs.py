"""Add team_configs table — coordinator/worker label registry per repo.

Task C of the combined ADR-0085+0086 plan. The table is keyed on
``repo`` (UNIQUE) and carries the coordinator label + the set of worker
labels for that repo. The ``GET /api/v1/queue_depth`` endpoint joins
through this table to exclude coordinator-authored tasks from the
"visible" / "in_flight" counts.

Revision ID: 20260609_1000
Revises: 20260608_2200
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP, UUID


revision: str = "20260609_1000"
down_revision: Union[str, Sequence[str], None] = "20260609_0900"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_configs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("repo", sa.String(255), nullable=False, unique=True),
        sa.Column("coordinator_label", sa.String(64), nullable=False),
        sa.Column(
            "worker_labels",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "created_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("team_configs")
