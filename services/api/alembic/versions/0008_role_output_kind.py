"""roles.output_kind column per ADR-0022.

ADR-0022 introduces ``Role.output_kind`` as the runner's dispatch key for
its post-Claude-Code disposition. Today's runner hard-codes a diff →
commit → push → PR flow that's correct for ``role-code-author`` but wrong
for every other seeded role; the autoscaler-driven smoke surfaced this
when wf-review failed with "Claude Code produced no changes to commit".

The column is required (NOT NULL) with no schema default — every role
must declare its kind. This migration:

  1. Adds the column nullable so existing rows survive the schema change.
  2. Backfills the eight seeded roles per ADR-0022's role-table mapping.
  3. Alters the column to NOT NULL after the backfill lands.

Downgrade drops the column.

Per ADR-0022 §"Migration of seeded roles", the eight starter roles
classify as:

  * role-code-author        → code      (today's runner behavior)
  * role-doc-author         → plan_doc  (diff confined to docs/plans/)
  * role-planner            → analysis  (produces task_directive only)
  * role-reviewer           → review    (posts gh pr review)
  * role-validator          → analysis  (placeholder; Ralph-loop ADR
                                         will reclassify)
  * role-feedback-analyzer  → analysis
  * role-ci-analyzer        → analysis
  * role-conflict-analyzer  → analysis

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Backfill mapping per ADR-0022's "Migration of seeded roles" table.
# Keep ordered + grouped by kind so future readers can re-derive the
# rationale from a single glance at this list.
_ROLE_KIND_BACKFILL: list[tuple[str, str]] = [
    # CODE — diff/commit/push/PR. Today's runner behavior, unchanged.
    ("role-code-author", "code"),

    # PLAN_DOC — like code but the diff is confined to docs/plans/.
    ("role-doc-author", "plan_doc"),

    # REVIEW — empty diff is success; runner posts gh pr review.
    ("role-reviewer", "review"),

    # ANALYSIS — empty diff is success; output flows to a downstream
    # step via the ADR-0015 step-output composition.
    ("role-planner", "analysis"),
    ("role-validator", "analysis"),  # placeholder; Ralph-loop ADR will reclassify
    ("role-feedback-analyzer", "analysis"),
    ("role-ci-analyzer", "analysis"),
    ("role-conflict-analyzer", "analysis"),
]


def upgrade() -> None:
    # 1. Add nullable so the column appears without breaking existing rows.
    op.add_column(
        "roles",
        sa.Column("output_kind", sa.String(length=32), nullable=True),
    )

    # 2. Backfill the eight seeded roles. The UPDATE is a no-op on any
    # role row that isn't one of the seeded ids (e.g. test fixtures created
    # at runtime); those rows are handled by the NOT NULL alter below
    # raising a clean error — the operator must seed them before upgrading.
    for role_id, kind in _ROLE_KIND_BACKFILL:
        op.execute(
            sa.text(
                "UPDATE roles SET output_kind = :kind WHERE id = :role_id"
            ).bindparams(kind=kind, role_id=role_id)
        )

    # 3. Default any remaining unclassified rows to ``analysis`` (the
    # safest fallback — empty diff is success, no PR-side side effect).
    # In practice, a fresh install has no roles at migration time and the
    # backfill above touches zero rows; on a partially-seeded install
    # this catch-all keeps the NOT NULL alter from erroring on operator-
    # added test fixtures. Seeded roles are already classified.
    op.execute(
        sa.text(
            "UPDATE roles SET output_kind = 'analysis' WHERE output_kind IS NULL"
        )
    )

    # 4. Alter to NOT NULL. Now that every row has a value, this is safe.
    op.alter_column("roles", "output_kind", nullable=False)


def downgrade() -> None:
    op.drop_column("roles", "output_kind")
