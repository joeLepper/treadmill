"""task_executions + llm_calls — ADR-0087 Phase 3 schema.

Creates the two new execution-tracking tables and updates the
``task_status`` VIEW to prefer ``task_executions``-derived status over
the legacy ``workflow_runs``-derived status during the Phase 3→4
transition window.

``task_executions`` replaces ``workflow_runs`` + ``workflow_run_steps``
as the coordinator's lifecycle-write target. One row per worker dispatch
cycle; the trigger column records why the worker was (re-)dispatched.

``llm_calls`` records per-subprocess token consumption, FK to
``task_executions`` with ON DELETE CASCADE.

The updated VIEW is additive: when a ``task_executions`` row exists for a
task it drives the status; when none exists the existing
``workflow_runs``-based clauses still apply (tasks dispatched by the old
autoscaler path or the legacy coordinator path remain correctly
classified). Tasks dispatched by ADR-0087 coordinators will never have
``workflow_runs`` rows (``dispatch_task`` was removed in PR-A), so the
two paths do not overlap — the preference clause is a safety net rather
than a regular-path merge.

Revision ID: 20260610_1000
Revises: 20260610_0900
Create Date: 2026-06-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260610_1000"
down_revision: Union[str, None] = "20260610_0900"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Upgrade VIEW: adds task_executions-derived clauses ────────────────────
#
# Two new LATERAL columns added to the FROM clause:
#   lr — most recent workflow_run (unchanged)
#   lte — most recent task_execution (new)
#
# Clause ordering changes:
#   3   — registered: now guards BOTH lr.run_id IS NULL AND lte.id IS NULL
#   3b  — pr_merged precedence (unchanged)
#   3a  — NEW: task_execution running → worker_label + ': executing'
#   3c  — NEW: task_execution failed  → worker_label + ': failed'
#   4-6 — workflow_run / PR lifecycle (unchanged)
#
_VIEW_UPGRADE = """
CREATE VIEW task_status AS
SELECT
    t.id,
    t.plan_id,
    t.repo,
    t.title,
    CASE
        -- 1. Cancelled: explicit cancellation event recorded (highest priority).
        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'task'
              AND e.action = 'cancelled'
        ) THEN 'cancelled'

        -- 2. Blocked: at least one dependency expression is not yet satisfied.
        WHEN EXISTS (
            SELECT 1 FROM task_dependencies d
            WHERE d.task_id = t.id
            AND NOT (
                CASE
                    WHEN split_part(d.expression, '.', 1) = 'task'
                         AND array_length(string_to_array(d.expression, '.'), 1) = 3
                         AND split_part(d.expression, '.', 3) = 'pr_merged'
                    THEN EXISTS (
                        SELECT 1 FROM events e
                        WHERE e.task_id = split_part(d.expression, '.', 2)::uuid
                          AND e.entity_type = 'github'
                          AND e.action = 'pr_merged'
                    )

                    WHEN split_part(d.expression, '.', 1) = 'task'
                         AND array_length(string_to_array(d.expression, '.'), 1) = 4
                         AND split_part(d.expression, '.', 3) = 'run'
                         AND split_part(d.expression, '.', 4) = 'completed'
                    THEN EXISTS (
                        SELECT 1 FROM workflow_runs r
                        WHERE r.task_id = split_part(d.expression, '.', 2)::uuid
                          AND EXISTS (
                              SELECT 1 FROM workflow_run_steps s
                              WHERE s.run_id = r.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM workflow_run_steps s
                              WHERE s.run_id = r.id
                                AND s.status NOT IN ('completed', 'cancelled')
                          )
                    )

                    WHEN split_part(d.expression, '.', 1) = 'task'
                         AND array_length(string_to_array(d.expression, '.'), 1) >= 5
                         AND split_part(d.expression, '.', 3) = 'step'
                         AND split_part(
                                 d.expression, '.',
                                 array_length(string_to_array(d.expression, '.'), 1)
                             ) = 'completed'
                    THEN EXISTS (
                        SELECT 1 FROM workflow_run_steps s
                        JOIN workflow_runs r ON r.id = s.run_id
                        WHERE r.task_id = split_part(d.expression, '.', 2)::uuid
                          AND s.step_name = substring(
                                  d.expression
                                  FROM E'task\\.[^.]+\\.step\\.(.+)\\.completed$'
                              )
                          AND s.status = 'completed'
                    )

                    ELSE FALSE
                END
            )
        ) THEN 'blocked'

        -- 3. No workflow_run AND no task_execution: task in initial registered state.
        WHEN lr.run_id IS NULL AND lte.id IS NULL THEN 'registered'

        -- 3b. github.pr_merged takes precedence over both task_execution state and
        --     any pending workflow step. With ADR-0087, lr.run_id IS NULL for new
        --     tasks so NOT EXISTS (failed step) is vacuously true; pr_merged always
        --     fires here when the event exists.
        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) AND NOT EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id
              AND s.status = 'failed'
        ) THEN 'pr_merged'

        -- 3a. ADR-0087 task_execution running (coordinator dispatched the worker).
        WHEN lte.id IS NOT NULL AND lte.status = 'running'
            THEN lte.worker_label || ': executing'

        -- 3c. ADR-0087 task_execution failed.
        WHEN lte.id IS NOT NULL AND lte.status = 'failed'
            THEN lte.worker_label || ': failed'

        -- 4. Most recent workflow_run has active steps (legacy dispatch path —
        --     present during Phase 3→4 transition).
        WHEN EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id
              AND s.status IN ('running', 'pending')
        ) THEN lr.workflow_id || ': executing'

        -- 5. Most recent workflow_run has failed steps. Overlay PR lifecycle as context.
        WHEN EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id AND s.status = 'failed'
        ) AND NOT EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id AND s.status IN ('running', 'pending')
        ) THEN (
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.task_id = t.id
                      AND e.entity_type = 'github'
                      AND e.action = 'pr_merged'
                ) THEN 'pr_merged (' || lr.workflow_id || ': failed)'

                WHEN EXISTS (
                    SELECT 1 FROM task_prs tp
                    WHERE tp.task_id = t.id
                ) AND NOT EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.task_id = t.id
                      AND e.entity_type = 'github'
                      AND e.action = 'pr_merged'
                ) THEN 'pr_opened (' || lr.workflow_id || ': failed)'

                ELSE lr.workflow_id || ': failed'
            END
        )

        -- 6a. review_passed (PRE-merge): open PR, not merged, latest run is
        --     wf-review with a completed step decision='approved'.
        WHEN NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) AND EXISTS (
            SELECT 1 FROM task_prs tp
            WHERE tp.task_id = t.id
        ) AND lr.workflow_id = 'wf-review' AND EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id
              AND s.status = 'completed'
              AND s.output ? 'decision'
              AND s.output->>'decision' = 'approved'
        ) THEN 'review_passed'

        -- 6b. PR open (task_prs row exists, no pr_merged event).
        WHEN EXISTS (
            SELECT 1 FROM task_prs tp
            WHERE tp.task_id = t.id
        ) AND NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) THEN 'pr_opened'

        -- 6c. PR merged — authoritative. With clause 3b above, this branch is
        --     effectively dead code for ADR-0087 tasks; kept so the downgrade
        --     restores the prior VIEW byte-identical.
        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) THEN 'pr_merged'

        -- 6d. No PR at all. Latest run's completed step with decision='fail'
        --     is a silent-fail; otherwise work finished locally → 'done'.
        ELSE (
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM workflow_run_steps s
                    WHERE s.run_id = lr.run_id
                      AND s.status = 'completed'
                      AND s.output ? 'decision'
                      AND s.output->>'decision' = 'fail'
                ) THEN lr.workflow_id || ': failed'
                ELSE 'done'
            END
        )

    END AS derived_status
FROM tasks t
LEFT JOIN LATERAL (
    SELECT r.id AS run_id, wv.workflow_id
    FROM workflow_runs r
    JOIN workflow_versions wv ON wv.id = r.workflow_version_id
    WHERE r.task_id = t.id
    ORDER BY r.created_at DESC
    LIMIT 1
) lr ON true
LEFT JOIN LATERAL (
    SELECT te.id, te.status, te.worker_label
    FROM task_executions te
    WHERE te.task_id = t.id
    ORDER BY te.started_at DESC
    LIMIT 1
) lte ON true;
"""

# ── Downgrade VIEW: restores 20260610_0900's view exactly ────────────────
#
# Clause 3 reverts to ``lr.run_id IS NULL`` (no lte check).
# Clauses 3a and 3c removed. lte LATERAL removed.
#
_VIEW_DOWNGRADE = """
CREATE VIEW task_status AS
SELECT
    t.id,
    t.plan_id,
    t.repo,
    t.title,
    CASE
        -- 1. Cancelled: explicit cancellation event recorded (highest priority).
        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'task'
              AND e.action = 'cancelled'
        ) THEN 'cancelled'

        -- 2. Blocked: at least one dependency expression is not yet satisfied.
        WHEN EXISTS (
            SELECT 1 FROM task_dependencies d
            WHERE d.task_id = t.id
            AND NOT (
                CASE
                    WHEN split_part(d.expression, '.', 1) = 'task'
                         AND array_length(string_to_array(d.expression, '.'), 1) = 3
                         AND split_part(d.expression, '.', 3) = 'pr_merged'
                    THEN EXISTS (
                        SELECT 1 FROM events e
                        WHERE e.task_id = split_part(d.expression, '.', 2)::uuid
                          AND e.entity_type = 'github'
                          AND e.action = 'pr_merged'
                    )

                    WHEN split_part(d.expression, '.', 1) = 'task'
                         AND array_length(string_to_array(d.expression, '.'), 1) = 4
                         AND split_part(d.expression, '.', 3) = 'run'
                         AND split_part(d.expression, '.', 4) = 'completed'
                    THEN EXISTS (
                        SELECT 1 FROM workflow_runs r
                        WHERE r.task_id = split_part(d.expression, '.', 2)::uuid
                          AND EXISTS (
                              SELECT 1 FROM workflow_run_steps s
                              WHERE s.run_id = r.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM workflow_run_steps s
                              WHERE s.run_id = r.id
                                AND s.status NOT IN ('completed', 'cancelled')
                          )
                    )

                    WHEN split_part(d.expression, '.', 1) = 'task'
                         AND array_length(string_to_array(d.expression, '.'), 1) >= 5
                         AND split_part(d.expression, '.', 3) = 'step'
                         AND split_part(
                                 d.expression, '.',
                                 array_length(string_to_array(d.expression, '.'), 1)
                             ) = 'completed'
                    THEN EXISTS (
                        SELECT 1 FROM workflow_run_steps s
                        JOIN workflow_runs r ON r.id = s.run_id
                        WHERE r.task_id = split_part(d.expression, '.', 2)::uuid
                          AND s.step_name = substring(
                                  d.expression
                                  FROM E'task\\.[^.]+\\.step\\.(.+)\\.completed$'
                              )
                          AND s.status = 'completed'
                    )

                    ELSE FALSE
                END
            )
        ) THEN 'blocked'

        -- 3. No workflow runs created yet.
        WHEN lr.run_id IS NULL THEN 'registered'

        -- 3b. github.pr_merged precedence over a pending step.
        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) AND NOT EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id
              AND s.status = 'failed'
        ) THEN 'pr_merged'

        -- 4. Most recent run has active steps. Show workflow slug as prefix.
        WHEN EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id
              AND s.status IN ('running', 'pending')
        ) THEN lr.workflow_id || ': executing'

        -- 5. Most recent run has failed steps. Overlay PR lifecycle as context.
        WHEN EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id AND s.status = 'failed'
        ) AND NOT EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id AND s.status IN ('running', 'pending')
        ) THEN (
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.task_id = t.id
                      AND e.entity_type = 'github'
                      AND e.action = 'pr_merged'
                ) THEN 'pr_merged (' || lr.workflow_id || ': failed)'

                WHEN EXISTS (
                    SELECT 1 FROM task_prs tp
                    WHERE tp.task_id = t.id
                ) AND NOT EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.task_id = t.id
                      AND e.entity_type = 'github'
                      AND e.action = 'pr_merged'
                ) THEN 'pr_opened (' || lr.workflow_id || ': failed)'

                ELSE lr.workflow_id || ': failed'
            END
        )

        -- 6a. review_passed (PRE-merge).
        WHEN NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) AND EXISTS (
            SELECT 1 FROM task_prs tp
            WHERE tp.task_id = t.id
        ) AND lr.workflow_id = 'wf-review' AND EXISTS (
            SELECT 1 FROM workflow_run_steps s
            WHERE s.run_id = lr.run_id
              AND s.status = 'completed'
              AND s.output ? 'decision'
              AND s.output->>'decision' = 'approved'
        ) THEN 'review_passed'

        -- 6b. PR open.
        WHEN EXISTS (
            SELECT 1 FROM task_prs tp
            WHERE tp.task_id = t.id
        ) AND NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) THEN 'pr_opened'

        -- 6c. PR merged.
        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) THEN 'pr_merged'

        -- 6d. No PR; check for silent-fail decision.
        ELSE (
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM workflow_run_steps s
                    WHERE s.run_id = lr.run_id
                      AND s.status = 'completed'
                      AND s.output ? 'decision'
                      AND s.output->>'decision' = 'fail'
                ) THEN lr.workflow_id || ': failed'
                ELSE 'done'
            END
        )

    END AS derived_status
FROM tasks t
LEFT JOIN LATERAL (
    SELECT r.id AS run_id, wv.workflow_id
    FROM workflow_runs r
    JOIN workflow_versions wv ON wv.id = r.workflow_version_id
    WHERE r.task_id = t.id
    ORDER BY r.created_at DESC
    LIMIT 1
) lr ON true;
"""


def upgrade() -> None:
    # ── 1. New tables ────────────────────────────────────────────────────
    op.create_table(
        "task_executions",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "task_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id"),
            nullable=False,
        ),
        sa.Column("worker_label", sa.Text(), nullable=False),
        sa.Column(
            "trigger",
            sa.Text(),
            sa.CheckConstraint(
                "trigger IN ('initial','coordinator-rework','evaluator-rework','peer-review')",
                name="ck_task_executions_trigger",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint(
                "status IN ('running','completed','failed')",
                name="ck_task_executions_status",
            ),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "task_id",
            "trigger",
            "worker_label",
            "started_at",
            name="uq_task_executions_spawn",
        ),
    )
    op.create_index(
        "ix_task_executions_task_id",
        "task_executions",
        ["task_id"],
    )

    op.create_table(
        "llm_calls",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "task_execution_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_creation_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_llm_calls_task_execution_id",
        "llm_calls",
        ["task_execution_id"],
    )

    # ── 2. Update task_status VIEW ───────────────────────────────────────
    op.execute("DROP VIEW IF EXISTS task_status")
    op.execute(_VIEW_UPGRADE.strip())


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS task_status")
    op.drop_index("ix_llm_calls_task_execution_id", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_index("ix_task_executions_task_id", table_name="task_executions")
    op.drop_table("task_executions")
    op.execute(_VIEW_DOWNGRADE.strip())
