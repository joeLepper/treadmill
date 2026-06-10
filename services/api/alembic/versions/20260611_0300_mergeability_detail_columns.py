"""Re-project task_mergeability detail columns — ADR-0087 Phase 5 hotfix.

The Phase 4 rewrite of ``task_mergeability`` (20260611_0100) preserved
the VIEW's decision logic but narrowed its projected columns to
``task_id, repo, pr_number, derived_mergeability``. The pre-ADR-0087
VIEW also projected ``head_sha``, ``review_decision``,
``validate_decision``, ``ci_conclusion``, ``pr_conflicting`` — and two
live consumers still SELECT them: ``GET /tasks/{id}/mergeability``
(``MergeabilityResponse``) and the dashboard overview SQL
(``tm.head_sha AS pr_head_sha``). Both 500'd with UndefinedColumnError
after the Phase 5 deploy (first surfaced on dev 2026-06-10 ~06:5xZ).

This migration recreates the VIEW with the SAME decision logic plus the
five detail columns projected from the LATERALs that already compute
them. No semantic change to ``derived_mergeability``.

Revision ID: 20260611_0300
Revises: 20260611_0200
Create Date: 2026-06-11
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260611_0300"
down_revision: Union[str, None] = "20260611_0200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TASK_MERGEABILITY_VIEW_FULL = """
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
         AND (validate.decision = 'pass' OR validate.decision IS NULL)
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
-- review: newest signal between a task.evaluator_verdict event
-- (ADR-0087 §Task execution flow step 6 — verdict 'approve' maps to
-- 'approved', 'rework' to 'changes_requested') and a review.override
-- event (the orchestrator's manual override, ADR-0042 lineage). The
-- wf-review workflow_run_steps branch is gone with the table.
-- Evaluator verdicts are task-scoped (the verdict format carries
-- pr_number + task_id, not a commit SHA), so unlike the pre-ADR-0087
-- VIEW there is no per-SHA pinning on the verdict branch; the
-- coordinator re-briefs the evaluator after any post-verdict push,
-- producing a fresh verdict event that wins by recency.
LEFT JOIN LATERAL (
    SELECT decision FROM (
        SELECT
            'approved'::text AS decision,
            e_ovr.created_at AS ts
        FROM events e_ovr
        WHERE e_ovr.entity_type = 'review'
          AND e_ovr.action = 'override'
          AND e_ovr.task_id = t.id
          AND (e_ovr.payload->>'commit_sha') = head.head_sha
        UNION ALL
        SELECT
            CASE (e_ev.payload->>'verdict')
                WHEN 'approve' THEN 'approved'::text
                WHEN 'rework'  THEN 'changes_requested'::text
                ELSE NULL
            END AS decision,
            e_ev.created_at AS ts
        FROM events e_ev
        WHERE e_ev.entity_type = 'task'
          AND e_ev.action = 'evaluator_verdict'
          AND e_ev.task_id = t.id
    ) AS review_sources
    WHERE decision IS NOT NULL
    ORDER BY ts DESC NULLS LAST
    LIMIT 1
) review ON true
-- validate: override-events-only post-ADR-0087. The wf-validate
-- workflow_run_steps branch is gone with the table; the evaluator's
-- holistic judgment subsumes the old validation gates (ADR-0029
-- superseded per ADR-0087 supersession map). A validate.override
-- event still forces 'pass' for operator recovery flows.
LEFT JOIN LATERAL (
    SELECT
        'pass'::text AS decision
    FROM events e_val
    WHERE e_val.entity_type = 'validate'
      AND e_val.action = 'override'
      AND e_val.task_id = t.id
      AND (e_val.payload->>'commit_sha') = head.head_sha
    ORDER BY e_val.created_at DESC
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
    op.execute("DROP VIEW IF EXISTS task_mergeability")
    op.execute(_TASK_MERGEABILITY_VIEW_FULL.strip())


def downgrade() -> None:
    raise NotImplementedError(
        "forward-only hotfix — the narrow-projection VIEW broke live "
        "consumers; there is nothing to go back to."
    )
