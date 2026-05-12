"""task_status VIEW — last-meaningful-lifecycle-event status per ADR-0011.

Cribbed from bunkhouse migration 020 (the post-evolution form after three
rounds of bug fixes). Five priority categories evaluated in order:

    cancelled > blocked > registered > <wf>: executing > pr_state / done

Active states carry a workflow-id prefix (``coding: executing``); failure
states overlay PR lifecycle (``pr_merged (review: failed)``) so a PR-merged-
but-review-failed task does not collapse to a generic ``failed``.

Adjustments from bunkhouse migration 020:
  - Treadmill tasks reference ``workflow_version_id``; the VIEW joins
    ``workflow_versions`` to expose the workflow slug.
  - GitHub PR-merged events use ``entity_type='github', action='pr_merged'``
    (Pattern B in plan running log) rather than bunkhouse's normalized
    ``entity_type='task', action='event.pr_merged'``.
  - The dependency-evaluation clause covers only ``task.<id>.pr_merged``,
    ``task.<id>.run.completed``, and ``task.<id>.step.<name>.completed``
    in v0. Unknown / future expression types (``task.<id>.event.<name>``,
    ``deployment.<env>.<repo>.<status>``) evaluate to FALSE — the safe
    forward-compatible default. ADR-0007's deployment clause lands when
    the deployments table ships.

Revision ID: 0002
Revises: 68593b5775c1
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "68593b5775c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TASK_STATUS_VIEW_SQL = """
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
        --    Satisfaction is evaluated inline. v0 supports:
        --      task.<id>.pr_merged
        --      task.<id>.run.completed
        --      task.<id>.step.<name>.completed
        --    Other expressions (event, deployment) evaluate to FALSE.
        WHEN EXISTS (
            SELECT 1 FROM task_dependencies d
            WHERE d.task_id = t.id
            AND NOT (
                CASE
                    -- task.<id>.pr_merged
                    WHEN split_part(d.expression, '.', 1) = 'task'
                         AND array_length(string_to_array(d.expression, '.'), 1) = 3
                         AND split_part(d.expression, '.', 3) = 'pr_merged'
                    THEN EXISTS (
                        SELECT 1 FROM events e
                        WHERE e.task_id = split_part(d.expression, '.', 2)::uuid
                          AND e.entity_type = 'github'
                          AND e.action = 'pr_merged'
                    )

                    -- task.<id>.run.completed
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

                    -- task.<id>.step.<name>.completed
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

                    -- Unknown expression type — treat as unsatisfied.
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
                -- PR was already merged before the run failed (e.g. review crash).
                WHEN EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.task_id = t.id
                      AND e.entity_type = 'github'
                      AND e.action = 'pr_merged'
                ) THEN 'pr_merged (' || lr.workflow_id || ': failed)'

                -- PR is open (task_prs row exists, not yet merged).
                WHEN EXISTS (
                    SELECT 1 FROM task_prs tp
                    WHERE tp.task_id = t.id
                ) AND NOT EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.task_id = t.id
                      AND e.entity_type = 'github'
                      AND e.action = 'pr_merged'
                ) THEN 'pr_opened (' || lr.workflow_id || ': failed)'

                -- No PR involved — bare workflow failure.
                ELSE lr.workflow_id || ': failed'
            END
        )

        -- 6. Most recent run is not active and not failed.
        --    Derive status from the PR lifecycle.

        -- 6a. PR is open (task_prs row exists, no pr_merged event).
        WHEN EXISTS (
            SELECT 1 FROM task_prs tp
            WHERE tp.task_id = t.id
        ) AND NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) THEN 'pr_opened'

        -- 6b. PR merged + most recent run was 'review' workflow → review passed.
        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) AND lr.workflow_id = 'wf-review' THEN 'review_passed'

        -- 6c. PR merged, latest run was something other than 'wf-review'.
        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) THEN 'pr_merged'

        -- 6d. No PR at all — all work finished locally.
        ELSE 'done'

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
    op.execute(_TASK_STATUS_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS task_status;")
