"""Drop prod_promotions — ADR-0088 superseded by GitHub environment protection.

Operator directive 2026-06-11: deploy approval is the repo's own CI
concern (environment required-reviewers); Treadmill is team
orchestration. The table shipped hours earlier in 20260611_0400 and
held no production data.

Revision ID: 20260611_0500
Revises: 20260611_0400
Create Date: 2026-06-11
"""

from __future__ import annotations

from alembic import op

revision = "20260611_0500"
down_revision = "20260611_0400"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_prod_promotions_repo_created", if_exists=True)
    op.drop_table("prod_promotions", if_exists=True)


def downgrade() -> None:
    raise NotImplementedError("re-create via 20260611_0400 if ever needed")
