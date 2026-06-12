"""``task_prs.head_sha`` — the ADR-0063-deferred (repo, head_sha) lookup.

ADR-0090's CI-observer needs to attribute a completed check SUITE
(keyed by commit SHA) to a task. The existing bridge is
``task_prs (repo, pr_number) → task_id``; check-suite webhooks carry
``(repo, head_sha)``. This adds the nullable ``head_sha`` column the
resolver (``treadmill_api/resolvers.py::resolve_task_by_head_sha``)
matches on — ADR-0063 deferred exactly this FK-ish lookup, and the
ADR-0090 ci-observer task is its first consumer.

Nullable by design: every existing row (and rows from writers that do
not yet know the head) stays valid; the column is populated as writers
adopt it. Safe on an empty or partially-populated table.

Index: the resolver's hot path is ``WHERE repo = ? AND head_sha = ?
ORDER BY created_at DESC LIMIT 1`` — partial index over non-NULL
head_sha keeps it cheap without bloating over legacy rows (same
pattern as ``uq_llm_calls_transcript_request``, 20260611_0600).

Revision ID: 20260612_0200
Revises: 20260612_0100
Create Date: 2026-06-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260612_0200"
down_revision: Union[str, None] = "20260612_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_prs",
        sa.Column("head_sha", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_task_prs_repo_head_sha",
        "task_prs",
        ["repo", "head_sha"],
        postgresql_where=sa.text("head_sha IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_task_prs_repo_head_sha", table_name="task_prs")
    op.drop_column("task_prs", "head_sha")
