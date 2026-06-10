"""team_configs.evaluator_label — ADR-0087 per-repo evaluator role.

ADR-0087 introduces the evaluator as the third role in the per-repo team
(coordinator + evaluator + N workers). ``team_configs`` gains a single
``evaluator_label`` column to store the evaluator session label for each
team. Nullable so existing rows (pre-ADR-0087) survive the migration;
``treadmill team up`` populates the column when a team stands up.

Revision ID: 20260610_0900
Revises: 20260609_1000
Create Date: 2026-06-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260610_0900"
down_revision: Union[str, None] = "20260609_1000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "team_configs",
        sa.Column("evaluator_label", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("team_configs", "evaluator_label")
