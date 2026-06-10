"""prod_promotions table — ADR-0088 operator-gated prod promotions.

One row per proposal; the row is the current-status read the promote
workflow re-verifies against. Transitions are CAS-guarded in the router
(``WHERE status = :expected AND expires_at > now()``); the audit trail
lives in the events table (``prod_promotion.*``).

Revision ID: 20260611_0400
Revises: 20260611_0300
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260611_0400"
down_revision = "20260611_0300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prod_promotions",
        sa.Column(
            "proposal_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'proposed'"),
        ),
        sa.Column("bundle", JSONB, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_prod_promotions_repo_created",
        "prod_promotions",
        ["repo", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_prod_promotions_repo_created", if_exists=True)
    op.drop_table("prod_promotions", if_exists=True)
