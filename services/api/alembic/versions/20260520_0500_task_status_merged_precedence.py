"""task_status — pr_merged precedence; review_passed becomes pre-merge.

Clause 6 in ``0017`` derived ``review_passed`` for *any* task that had a
``github/pr_merged`` event AND whose latest run was ``wf-review`` — and that
fires *before* the ``pr_merged`` clause. Because the happy path is "clean
wf-review approval → auto-merge", and auto-merge creates no later workflow_run
(it is a Redis cooling-off + direct GitHub merge, see
``coordination/triggers.fire_elapsed_auto_merges``), the latest run stays
``wf-review`` forever. Net effect: every smoothly-merged task is labeled
``review_passed`` indefinitely instead of ``pr_merged``, and the clutter
regrows on every happy-path merge.

``review_passed`` is inherited bunkhouse vocabulary (ADR-0013) whose intended
meaning is "review passed, awaiting merge" — a *pre-merge* state (ADR-0031's
mergeability state machine: ``review_passed --> mergeable --> auto_merging``).
The ``0017`` implementation inverted it into a post-merge label.

This migration:
  * Makes ``pr_merged`` authoritative: any task with a ``github/pr_merged``
    event derives ``pr_merged`` regardless of which workflow ran last.
  * Redefines ``review_passed`` as the genuine pre-merge state: an open PR
    (``task_prs`` row, no ``pr_merged`` event) whose latest run is ``wf-review``
    with a completed step carrying ``output->>'decision' = 'approved'``. This
    is the transient "approved, inside the auto-merge cooling-off window" state.

Only the PR-lifecycle block (clause 6) changes. Clauses 1-5 (cancelled,
blocked, registered, executing, failed-overlay) and 6d (no-PR done/fail) are
preserved verbatim from 0017.

Revision ID: 20260520_0500
Revises: 20260519_1930
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op


revision: str = "20260520_0500"
down_revision: Union[str, Sequence[str], None] = "20260519_1930"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Shared head: clauses 1-5, identical to 0017. Section 6 differs per version.
_VIEW_PREAMBLE = """
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
"""

# Section 6 — PR lifecycle. NEW ordering: review_passed (pre-merge) → pr_opened
# → pr_merged → done/fail. pr_merged is authoritative whenever a merge event
# exists; review_passed requires the PR to NOT be merged.
_VIEW_SECTION6_NEW = """
        -- 6a. review_passed (PRE-merge): open PR, not merged, latest run is
        --     wf-review with a completed step decision='approved'. The
        --     transient "approved, awaiting auto-merge" cooling-off state.
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

        -- 6c. PR merged — authoritative regardless of which run was latest.
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
) lr ON true;
"""

# Section 6 as defined in 0017 (for downgrade): review_passed required a
# pr_merged event AND latest run wf-review, ordered before pr_merged.
_VIEW_SECTION6_0017 = """
        WHEN EXISTS (
            SELECT 1 FROM task_prs tp
            WHERE tp.task_id = t.id
        ) AND NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) THEN 'pr_opened'

        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) AND lr.workflow_id = 'wf-review' THEN 'review_passed'

        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) THEN 'pr_merged'

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
    op.execute("DROP VIEW IF EXISTS task_status;")
    op.execute(_VIEW_PREAMBLE + _VIEW_SECTION6_NEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS task_status;")
    op.execute(_VIEW_PREAMBLE + _VIEW_SECTION6_0017)
