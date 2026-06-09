"""task_status — pr_merged precedence over a pending step.

ADR-0085+0086 combined implementation, Task A.

After ``20260520_0500`` made ``pr_merged`` authoritative whenever a
``github.pr_merged`` event existed, one residual ordering bug remained:
clause 4 ("most recent run has active steps … `wf-X: executing`") fires
ABOVE the clause-6c ``pr_merged`` check. A coordinator-dispatched task
whose latest workflow_run has a freshly-INSERTed ``pending`` step (no
``started_at``) — typical of the ADR-0086 step-registration shape where
the coordinator creates a run and step row up-front before the worker
actually starts — therefore returns ``<workflow_id>: executing`` even
when a ``github.pr_merged`` event has already been recorded against the
same task.

Symptom: a merged PR shows as ``wf-author: executing`` indefinitely
because the pending step row never transitions; the user sees a
clearly-completed task labelled as in-flight, and the autoscaler can
double-count it.

Fix: insert one new clause between clause 3 (registered) and clause 4
(executing) that returns ``pr_merged`` whenever the ``github.pr_merged``
event exists, regardless of step state. Renumbered as clause "3b" in
inline comments so the existing 1/2/3/4/5/6 numbering is preserved.
This clause is structurally equivalent to clause 6c — the only
difference is precedence: 3b fires above executing, so a merged PR
wins over a stale pending step.

All other clauses (1, 2, 3, 4, 5, 6a–6d, including the 5/6 overlays
``pr_merged (<workflow_id>: failed)`` and ``pr_opened (<workflow_id>:
failed)``) are preserved verbatim from ``20260520_0500``. The downgrade
restores ``20260520_0500``'s view exactly.

Revision ID: 20260609_0900
Revises: 20260608_2200
Create Date: 2026-06-09
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260609_0900"
down_revision: Union[str, None] = "20260608_2200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Section A — clauses 1, 2, 3, and the NEW 3b (pr_merged precedence).
# Identical to the corresponding section of ``20260520_0500`` plus the
# inserted clause 3b. Sections B (clauses 4, 5) and C (clause 6) follow
# verbatim from the prior migration.
_VIEW_SECTION_PREAMBLE = """
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

        -- 3b. NEW: ``github.pr_merged`` precedence over a pending step.
        --     Coordinator-dispatched tasks (ADR-0086) create a run + a
        --     ``pending`` step up-front before the worker reports back;
        --     without this clause, clause 4 would mark the task
        --     ``<workflow_id>: executing`` even after the PR has merged.
        --     Structurally equivalent to clause 6c — only the precedence
        --     differs. The ``NOT EXISTS (failed step on latest run)``
        --     guard preserves clause 5's ``pr_merged (<wf>: failed)``
        --     overlay: a merged task with a failed step retains the
        --     downstream-failure context the overlay was built for.
        --     (Combined ADR-0085+0086 plan Task A.)
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
"""

# Section 6 — PR lifecycle. Copied verbatim from ``20260520_0500``.
_VIEW_SECTION_PR_LIFECYCLE = """
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
        --     With clause 3b above, this branch is effectively dead code
        --     (a merged PR has already returned via 3b); kept verbatim so
        --     the downgrade restores 0500's view byte-identical.
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

# Downgrade restores ``20260520_0500``'s view byte-identical: no 3b clause.
_VIEW_DOWNGRADE_PREAMBLE = """
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


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS task_status;")
    op.execute(_VIEW_SECTION_PREAMBLE + _VIEW_SECTION_PR_LIFECYCLE)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS task_status;")
    op.execute(_VIEW_DOWNGRADE_PREAMBLE + _VIEW_SECTION_PR_LIFECYCLE)
