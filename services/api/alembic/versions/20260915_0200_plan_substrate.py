"""plans: add ``substrate`` — per-plan coordination substrate (ADR-0118 SC6).

The router and the legacy agent-coordinator must never both act on one plan's events
(cross-substrate double-dispatch / split-brain). ADR-0118 SC6 binds the substrate PER PLAN
and IMMUTABLE at standup: a plan records whether the ROUTER or the LEGACY coordinator owns
it, the flag is resolved ONCE at standup and never re-read per event, and each substrate
ignores the other's plans. A repo's per-repo flag only selects the substrate for NEW
standups; flipping it mid-flight cannot move a running plan.

Default ``legacy`` so every existing plan stays on the agent-coordinator; the router claims
only plans explicitly stood up under it.

Revision ID: 20260915_0200
Revises: 20260915_0100
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260915_0200"
down_revision: Union[str, Sequence[str], None] = "20260915_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column(
            "substrate",
            sa.String(length=16),
            nullable=False,
            server_default="legacy",
        ),
    )
    op.create_check_constraint(
        "ck_plans_substrate",
        "plans",
        "substrate IN ('legacy', 'router')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_plans_substrate", "plans", type_="check")
    op.drop_column("plans", "substrate")
