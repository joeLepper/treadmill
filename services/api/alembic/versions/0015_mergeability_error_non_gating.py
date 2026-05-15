"""task_mergeability VIEW ignores verdict='error' (ADR-0039).

ADR-0039 decided that ``verdict='error'`` from any check does not gate
merge. The aggregate predicate narrows from
``(verdict IN ('fail','error') AND severity='blocking')``
to ``(verdict='fail' AND severity='blocking')``.

The original 0013 migration treated errors the same as fails; this
migration decouples them. Errors are logged with rule_id + reason for
ADR-0020 observability so operators can spot validator-quality drift.

The rest of the VIEW is unchanged. Downgrade restores 0013's behavior.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op


revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_VIEW_SQL = """
CREATE VIEW task_mergeability AS
SELECT
    t.id AS task_id,
    tp.repo,
    tp.pr_number,
    head.head_sha,
    review.decision AS review_decision,
    validate.decision AS validate_decision,
    ci.conclusion AS ci_conclusion,
    conflict.is_conflicting AS pr_conflicting,
    CASE
        WHEN head.head_sha IS NULL                              THEN 'pending'
        WHEN conflict.is_conflicting IS TRUE                    THEN 'blocked-on-conflict'
        WHEN ci.conclusion = 'failure'                          THEN 'blocked-on-ci'
        WHEN review.decision = 'changes_requested'              THEN 'blocked-on-review'
        WHEN review.decision = 'needs-more-info'                THEN 'blocked-on-review'
        WHEN validate.decision = 'fail'                         THEN 'blocked-on-validate'
        WHEN review.decision = 'approved'
         AND validate.decision = 'pass'
         AND (ci.conclusion = 'success' OR ci.conclusion IS NULL)
         AND conflict.is_conflicting IS NOT TRUE                THEN 'mergeable'
        ELSE 'pending'
    END AS derived_mergeability
FROM tasks t
JOIN task_prs tp ON tp.task_id = t.id
LEFT JOIN LATERAL (
    SELECT (e.payload->>'head_sha') AS head_sha
    FROM events e
    WHERE e.entity_type = 'github'
      AND e.action IN ('pr_opened', 'pr_synchronize')
      AND (e.payload->>'repo') = tp.repo
      AND (e.payload->>'pr_number')::int = tp.pr_number
    ORDER BY e.created_at DESC
    LIMIT 1
) head ON true
-- review: latest wf-review step.completed whose envelope's commit_sha
-- matches HEAD. (Unchanged from 0006.)
LEFT JOIN LATERAL (
    SELECT (s.output->>'decision') AS decision
    FROM workflow_run_steps s
    JOIN workflow_runs r ON r.id = s.run_id
    JOIN workflow_versions wv ON wv.id = r.workflow_version_id
    WHERE r.task_id = t.id
      AND wv.workflow_id = 'wf-review'
      AND s.status = 'completed'
      AND (s.output->>'commit_sha') = head.head_sha
    ORDER BY s.completed_at DESC NULLS LAST
    LIMIT 1
) review ON true
-- validate: per-check verdict aggregate (ADR-0039). Only verdict='fail'
-- gates merge; verdict='error' is logged for observability but does not
-- propagate to the aggregate. Compute in SQL from the checks array in the
-- step's output payload.
LEFT JOIN LATERAL (
    SELECT
        CASE
            WHEN EXISTS (
                SELECT 1
                FROM jsonb_array_elements(
                    COALESCE(s.output->'payload'->'checks', '[]'::jsonb)
                ) AS check_row
                WHERE (check_row->>'severity') = 'blocking'
                  AND (check_row->>'verdict') = 'fail'
            ) THEN 'fail'
            ELSE 'pass'
        END AS decision
    FROM workflow_run_steps s
    JOIN workflow_runs r ON r.id = s.run_id
    JOIN workflow_versions wv ON wv.id = r.workflow_version_id
    WHERE r.task_id = t.id
      AND wv.workflow_id = 'wf-validate'
      AND s.status = 'completed'
      AND (s.output->>'commit_sha') = head.head_sha
    ORDER BY s.completed_at DESC NULLS LAST
    LIMIT 1
) validate ON true
LEFT JOIN LATERAL (
    SELECT
        CASE WHEN EXISTS (
            SELECT 1 FROM events e2
            WHERE e2.entity_type = 'github'
              AND e2.action = 'check_run_completed'
              AND e2.commit_sha = head.head_sha
              AND (e2.payload->>'conclusion') IN ('failure', 'timed_out', 'action_required')
        ) THEN 'failure'
        WHEN EXISTS (
            SELECT 1 FROM events e2
            WHERE e2.entity_type = 'github'
              AND e2.action = 'check_run_completed'
              AND e2.commit_sha = head.head_sha
        ) THEN 'success'
        ELSE NULL
    END AS conclusion
) ci ON true
LEFT JOIN LATERAL (
    SELECT (e3.payload->>'is_conflicting')::boolean AS is_conflicting
    FROM events e3
    WHERE e3.entity_type = 'github'
      AND e3.action = 'pr_conflict'
      AND e3.commit_sha = head.head_sha
    ORDER BY e3.created_at DESC
    LIMIT 1
) conflict ON true;
"""

# 0013's body (from 0013 migration file).
_OLD_VIEW_SQL = """
CREATE VIEW task_mergeability AS
SELECT
    t.id AS task_id,
    tp.repo,
    tp.pr_number,
    head.head_sha,
    review.decision AS review_decision,
    validate.decision AS validate_decision,
    ci.conclusion AS ci_conclusion,
    conflict.is_conflicting AS pr_conflicting,
    CASE
        WHEN head.head_sha IS NULL                              THEN 'pending'
        WHEN conflict.is_conflicting IS TRUE                    THEN 'blocked-on-conflict'
        WHEN ci.conclusion = 'failure'                          THEN 'blocked-on-ci'
        WHEN review.decision = 'changes_requested'              THEN 'blocked-on-review'
        WHEN review.decision = 'needs-more-info'                THEN 'blocked-on-review'
        WHEN validate.decision = 'fail'                         THEN 'blocked-on-validate'
        WHEN review.decision = 'approved'
         AND validate.decision = 'pass'
         AND (ci.conclusion = 'success' OR ci.conclusion IS NULL)
         AND conflict.is_conflicting IS NOT TRUE                THEN 'mergeable'
        ELSE 'pending'
    END AS derived_mergeability
FROM tasks t
JOIN task_prs tp ON tp.task_id = t.id
LEFT JOIN LATERAL (
    SELECT (e.payload->>'head_sha') AS head_sha
    FROM events e
    WHERE e.entity_type = 'github'
      AND e.action IN ('pr_opened', 'pr_synchronize')
      AND (e.payload->>'repo') = tp.repo
      AND (e.payload->>'pr_number')::int = tp.pr_number
    ORDER BY e.created_at DESC
    LIMIT 1
) head ON true
LEFT JOIN LATERAL (
    SELECT (s.output->>'decision') AS decision
    FROM workflow_run_steps s
    JOIN workflow_runs r ON r.id = s.run_id
    JOIN workflow_versions wv ON wv.id = r.workflow_version_id
    WHERE r.task_id = t.id
      AND wv.workflow_id = 'wf-review'
      AND s.status = 'completed'
      AND (s.output->>'commit_sha') = head.head_sha
    ORDER BY s.completed_at DESC NULLS LAST
    LIMIT 1
) review ON true
LEFT JOIN LATERAL (
    SELECT
        CASE
            WHEN EXISTS (
                SELECT 1
                FROM jsonb_array_elements(
                    COALESCE(s.output->'payload'->'checks', '[]'::jsonb)
                ) AS check_row
                WHERE (check_row->>'severity') = 'blocking'
                  AND (check_row->>'verdict') IN ('fail', 'error')
            ) THEN 'fail'
            ELSE 'pass'
        END AS decision
    FROM workflow_run_steps s
    JOIN workflow_runs r ON r.id = s.run_id
    JOIN workflow_versions wv ON wv.id = r.workflow_version_id
    WHERE r.task_id = t.id
      AND wv.workflow_id = 'wf-validate'
      AND s.status = 'completed'
      AND (s.output->>'commit_sha') = head.head_sha
    ORDER BY s.completed_at DESC NULLS LAST
    LIMIT 1
) validate ON true
LEFT JOIN LATERAL (
    SELECT
        CASE WHEN EXISTS (
            SELECT 1 FROM events e2
            WHERE e2.entity_type = 'github'
              AND e2.action = 'check_run_completed'
              AND e2.commit_sha = head.head_sha
              AND (e2.payload->>'conclusion') IN ('failure', 'timed_out', 'action_required')
        ) THEN 'failure'
        WHEN EXISTS (
            SELECT 1 FROM events e2
            WHERE e2.entity_type = 'github'
              AND e2.action = 'check_run_completed'
              AND e2.commit_sha = head.head_sha
        ) THEN 'success'
        ELSE NULL
    END AS conclusion
) ci ON true
LEFT JOIN LATERAL (
    SELECT (e3.payload->>'is_conflicting')::boolean AS is_conflicting
    FROM events e3
    WHERE e3.entity_type = 'github'
      AND e3.action = 'pr_conflict'
      AND e3.commit_sha = head.head_sha
    ORDER BY e3.created_at DESC
    LIMIT 1
) conflict ON true;
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS task_mergeability;")
    op.execute(_NEW_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS task_mergeability;")
    op.execute(_OLD_VIEW_SQL)
