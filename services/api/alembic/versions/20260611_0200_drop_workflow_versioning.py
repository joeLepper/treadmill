"""Drop workflow versioning — ADR-0087 Phase 5.

Removes the last four tables of the pre-ADR-0087 execution model:

  workflows, workflow_versions,
  workflow_version_steps      — the workflow/version/step definition
                                layer; coordinator briefs + the evaluator
                                replace per-step workflow pipelines
  event_triggers              — mapped (repo, event_type) → workflow_id
                                for the consumer's trigger evaluator
                                (deleted in Phase 4); FK → workflows

Also drops ``tasks.workflow_version_id`` (NOT NULL, FK →
``workflow_versions`` ON DELETE RESTRICT — the RESTRICT means the
table drop is impossible without removing the column first). Tasks no
longer pin a workflow version; the coordinator decides how a task is
executed at dispatch time (ADR-0087 §Task execution flow).

No VIEW changes: the Phase 4 migration (20260611_0100) already
rewrote ``task_status`` and ``task_mergeability`` against the
ADR-0087 surface; neither references the tables dropped here.

No quiesce guard needed: unlike workflow_runs (which had a live
writer until the coordinator restart), these tables are
definition-data written only by the retired seed path and the
workflows router — both removed in this same PR. The Phase 4
migration's guard already forced the coordinator-restart ordering.

Downgrade: NOT SUPPORTED, same rationale as 20260611_0100 — the
definition rows (workflow versions pinned by years of run history)
cannot be honestly recreated empty. Restore from a database backup.

Revision ID: 20260611_0200
Revises: 20260611_0100
Create Date: 2026-06-11
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260611_0200"
down_revision: Union[str, None] = "20260611_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Children before parents (FKs):
#   event_triggers.workflow_id        → workflows (CASCADE)
#   workflow_version_steps.version_id → workflow_versions (CASCADE)
#   workflow_versions.workflow_id     → workflows (CASCADE)
_DROP_TABLES = (
    "event_triggers",
    "workflow_version_steps",
    "workflow_versions",
    "workflows",
)


def upgrade() -> None:
    # tasks.workflow_version_id first — its ON DELETE RESTRICT FK
    # blocks the workflow_versions drop. Index, then constraint-with-
    # column via raw DROP COLUMN (Postgres drops dependent constraints
    # with the column).
    op.execute("DROP INDEX IF EXISTS ix_tasks_workflow_version_id")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS workflow_version_id")

    for table in _DROP_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


def downgrade() -> None:
    raise NotImplementedError(
        "ADR-0087 Phase 5 drops are not reversible by migration — "
        "workflow definitions + the tasks.workflow_version_id pins "
        "cannot be honestly recreated empty. Restore from a database "
        "backup instead."
    )
