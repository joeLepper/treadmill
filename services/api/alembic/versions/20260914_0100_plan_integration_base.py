"""plans: add integration_base column (ADR-0114, amends ADR-0110).

The coordinator cuts its per-plan feature-branch off ``origin/main`` and drifts it
against ``origin/main``. Some plans must base on a NON-main ref — e.g. a plan whose
tasks read design docs that live only on ``joes-agents/<design-branch>``, or a
deliverable that layers on a base whose ``main`` we do not own (a collaborator repo we
must never touch the main of). This column stores the resolved per-plan base ref: NULL
means ``origin/main`` (the default, unchanged), a non-NULL ref names the base the
coordinator cuts + drifts against — and, when non-NULL, the integration branch itself
is the deliverable (no branch→main handoff PR).

Revision ID: 20260914_0100
Revises: 20260913_0100
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260914_0100"
down_revision: Union[str, Sequence[str], None] = "20260913_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("integration_base", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plans", "integration_base")
