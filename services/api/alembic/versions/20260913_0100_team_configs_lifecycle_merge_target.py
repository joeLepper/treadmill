"""team_configs.lifecycle + merge_target — ADR-0109 / ADR-0110 per-repo mode.

ADR-0109 makes teams ephemeral by default (stand up on the first plan, tear
down when the plan is done); ADR-0110 makes them integrate on a per-plan
``joes-agents/<slug>`` feature branch by default (the human owns the branch->main
PR). ``team_configs`` gains two columns to record each repo's chosen mode:

- ``lifecycle``     — ``ephemeral`` (default) | ``persistent`` | ``manual``
- ``merge_target``  — ``feature-branch`` (default) | ``main``

Both are NOT NULL with server defaults, so existing rows adopt the safe,
resource-conserving defaults without a data backfill. ``treadmill team up``
sets them explicitly when the operator overrides.

Revision ID: 20260913_0100
Revises: 20260814_0100
Create Date: 2026-09-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260913_0100"
down_revision: Union[str, None] = "20260814_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "team_configs",
        sa.Column(
            "lifecycle",
            sa.String(length=16),
            nullable=False,
            server_default="ephemeral",
        ),
    )
    op.add_column(
        "team_configs",
        sa.Column(
            "merge_target",
            sa.String(length=16),
            nullable=False,
            server_default="feature-branch",
        ),
    )


def downgrade() -> None:
    op.drop_column("team_configs", "merge_target")
    op.drop_column("team_configs", "lifecycle")
