"""Conflict-answer staleness vs base movement — task ce42dfed.

The lazy ``pr_conflicting`` resolver (task 536bf319, PR #320) persists a
(head_sha, base)-dependent answer as a ``github.pr_conflict`` event. Base
movement after a persisted ``false`` can create a REAL conflict that was
never re-checked: the old conflict-detection sweep re-checked every open
PR on each ``pr_merged`` and is gone (ADR-0087 Phase 5); the residual
guard was the merge attempt failing loudly at §9.3 — a LATE failure at
merge time instead of gate time. Both #320 reviewers flagged this
independently.

This migration recreates ``task_mergeability`` with one change to the
conflict LATERAL: an answer is trusted only if it POSTDATES the repo's
latest ``github.pr_merged`` event. A stale answer reads as NULL — which
is precisely the state the lazy resolver fires on, so the coordinator's
next poll re-derives a fresh answer at gate time. With no ``pr_merged``
in the repo yet, every answer stands (COALESCE to ``-infinity``).

``derived_mergeability``'s CASE is byte-identical — NULL conflict has
always passed the ``IS NOT TRUE`` arm; the gate-time re-check rides the
coordinator's conflict-facet poll + resolver re-fire, exactly the #320
contract ("NULL means one more poll resolves it").

Revision ID: 20260612_0100
Revises: 20260611_0600
Create Date: 2026-06-12
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260612_0100"
down_revision: Union[str, None] = "20260611_0600"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VIEW_WITH_STALENESS = """
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
      -- Staleness rule (task ce42dfed): a conflict answer is
      -- (head, base)-dependent. Any pr_merged in the repo moves the
      -- base, so an answer computed BEFORE the repo's latest merge is
      -- unresolved (NULL) — the lazy resolver (task 536bf319) re-fires
      -- on the coordinator's next poll and persists a fresh answer
      -- that postdates the merge. No merges yet -> every answer stands.
      AND e3.created_at > COALESCE((
          SELECT MAX(em.created_at)
          FROM events em
          WHERE em.entity_type = 'github'
            AND em.action = 'pr_merged'
            AND (em.payload->>'repo') = tp.repo
      ), '-infinity'::timestamptz)
    ORDER BY e3.created_at DESC
    LIMIT 1
) conflict ON true;
"""

_VIEW_WITHOUT_STALENESS = """
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
    op.execute(_VIEW_WITH_STALENESS.strip())


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS task_mergeability")
    op.execute(_VIEW_WITHOUT_STALENESS.strip())
