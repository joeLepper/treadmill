"""events.commit_sha column + partial indexes per ADR-0014.

ADR-0013's ``task_mergeability`` VIEW joins workflow outputs and webhook
events on ``commit_sha``. Today the SHA is buried in ``events.payload``
JSONB; the VIEW would need a JSON extraction on the hot join path. ADR-
0011's JSONB discipline cautions against precisely that pattern.

ADR-0014 promotes ``commit_sha`` to a real column on ``events`` with two
partial indexes:

  * ``(task_id, commit_sha) WHERE commit_sha IS NOT NULL`` — "did this
    task ever run anything against this HEAD?"
  * ``(entity_type, action, commit_sha) WHERE commit_sha IS NOT NULL`` —
    "is there a ``github.check_run_completed`` at this HEAD?" (the VIEW
    pattern for CI status).

Partial indexes keep the index small over the NULL majority (every
``plan.registered`` and other pre-commit event is NULL).

The column is forward-only: v0 has no production data, so no backfill is
needed. Future production migrations may need a backfill script reading
``payload->>'head_sha'`` per event-type; documented as a follow-up for
the production-deploy ADR.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("commit_sha", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_events_task_commit",
        "events",
        ["task_id", "commit_sha"],
        postgresql_where=sa.text("commit_sha IS NOT NULL"),
    )
    op.create_index(
        "ix_events_entity_action_commit",
        "events",
        ["entity_type", "action", "commit_sha"],
        postgresql_where=sa.text("commit_sha IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_events_entity_action_commit", table_name="events")
    op.drop_index("ix_events_task_commit", table_name="events")
    op.drop_column("events", "commit_sha")
