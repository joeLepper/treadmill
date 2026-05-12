"""task_mergeability VIEW — per-commit mergeability projection per ADR-0013.

ADR-0011 commits Treadmill to derived status as VIEWs. ADR-0013 extends
that commitment with a sibling VIEW (next to ``task_status``) that
answers a different question: *is the diff at the HEAD of this task's
PR currently mergeable?*

Mergeability is a join of four async signals at the **current HEAD**:

  * ``wf-review`` step output      (decision = ``approved`` | ``changes_requested`` | ``needs-more-info``)
  * ``wf-validate`` step output    (decision = ``pass`` | ``fail`` | ``error``)
  * GitHub ``check_run_completed`` events (conclusion = ``success`` | ``failure`` | ...)
  * Conflict-sweep events          (``github.pr_conflict`` with ``is_conflicting``)

Every signal is filtered by the HEAD SHA from the latest ``pr_opened``
or ``pr_synchronize`` event. A new push (``pr_synchronize``) invalidates
prior thumbs *by construction* — the VIEW's filter no longer matches
the old SHA so review / validate fields become NULL until fresh thumbs
land at the new HEAD.

Priority order (most-severe-blocker wins, mirroring ``task_status``):

  pending(no-head) > conflict > ci-failure > review-changes-requested
  > review-needs-more-info > validate-fail > mergeable > pending(incomplete)

The CASE-WHEN in the VIEW *is* the mergeability state machine. Adding a
new blocker means adding one clause + one derived state. The VIEW is
the single seam.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TASK_MERGEABILITY_VIEW_SQL = """
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
-- head: most recent pr_opened or pr_synchronize for this (repo, pr_number).
-- The ``head_sha`` lives in the normalized payload (ADR-0013 ref) for
-- both verbs; ``events.commit_sha`` is also populated by the receiver
-- per ADR-0014, but the payload extraction is the canonical source
-- here because pr_opened / pr_synchronize *carry* the HEAD rather than
-- *running against* it.
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
-- review: latest wf-review step.completed whose envelope's
-- ``commit_sha`` matches HEAD. The ADR-0012 envelope promotes
-- ``commit_sha`` to a top-level field; ``output->>'commit_sha'`` is
-- the SQL contract.
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
-- validate: same shape, wf-validate.
LEFT JOIN LATERAL (
    SELECT (s.output->>'decision') AS decision
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
-- ci: aggregated check_run_completed events at HEAD.
--   any conclusion ∈ {failure, timed_out, action_required} → 'failure'
--   else any conclusion at all                             → 'success'
--   else                                                   → NULL ("no CI configured")
-- ``events.commit_sha`` is populated by the webhook receiver per
-- ADR-0014; the partial index ``ix_events_entity_action_commit``
-- accelerates this path.
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
-- conflict: latest github.pr_conflict event at HEAD with
-- ``is_conflicting`` flag in payload. Agent 3 (Phase B.3) ships the
-- ``GithubPrConflict`` event class + the sweep that emits it; until
-- then this LATERAL returns NULL and the VIEW's three-valued logic
-- handles it cleanly (``IS NOT TRUE`` is satisfied by NULL).
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
    op.execute(_TASK_MERGEABILITY_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS task_mergeability;")
