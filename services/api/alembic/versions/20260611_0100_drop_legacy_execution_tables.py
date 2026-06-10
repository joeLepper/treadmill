"""Drop legacy execution tables — ADR-0087 Phase 4.

Removes the fifteen tables of the pre-ADR-0087 execution model:

  workflow_runs, workflow_run_steps        — replaced by task_executions
  roles, role_versions, role_skills,
  role_hooks, skills, hooks                — role/skill/hook prompt machinery;
                                             worker CLAUDE.md templates replace it
  task_validations                         — evaluator holistic judgment replaces
                                             per-PR validation gates (ADR-0029 out)
  architect_gold_rows, validator_gold_rows,
  review_dspy_variant_pr, triage_findings  — DSPy corpora; task-tailored briefs
                                             replace standardized prompt tuning
  workflow_dispatch_dedup                  — dispatch_task dedup guard; the
                                             dispatcher path was removed in PR-A/PR-F

(`output_kind` from the ADR's delete list is a COLUMN on roles, not a
table — it goes down with the roles table.)

Also rewrites the two VIEWs that referenced the dropped tables:

* ``task_status`` — ADR-0087-only: the workflow_run LATERAL + clauses
  4–6 are gone; task_executions drives the execution-state clauses.
  ``run.completed`` / ``step.<name>.completed`` dependency expressions
  are re-interpreted against task_executions (a completed execution
  with no running sibling satisfies them); ``pr_merged`` expressions
  are unchanged (events-table).
* ``task_mergeability`` — the wf-review / wf-validate
  workflow_run_steps UNION branches in the review + validate LATERALs
  are gone. Review decisions now come from ``task.evaluator_verdict``
  events (verdict ``approve`` → 'approved', ``rework`` →
  'changes_requested'); ``review.override`` events still count.
  Validate decisions are override-events-only (the evaluator's
  holistic judgment subsumes wf-validate). NOTE: ADR-0087 §Keep listed
  ``task_mergeability`` as unchanged — that was wrong; the VIEW
  queried three dropped tables and had to be rewritten here.

Precondition guard (ADR-0087 Phase 4 + plan PR-F scope): before
dropping, the migration takes ``LOCK TABLE workflow_runs IN EXCLUSIVE
MODE NOWAIT`` — failing immediately if an active coordinator holds a
write transaction — then checks ``MAX(created_at)``. A row inserted
within the last ``DEPRECATED_TABLE_QUIESCE_SECONDS`` (env; default
300) aborts the migration with instructions to restart the live
coordinator sessions. Loud failure instead of dropping a table with
an active writer.

Downgrade: NOT SUPPORTED. The dropped tables carry years of corpus +
run history; recreating empty schemas would satisfy alembic but lie
about the data. Restore from a database backup instead.

Revision ID: 20260611_0100
Revises: 20260610_1000
Create Date: 2026-06-11
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260611_0100"
down_revision: Union[str, None] = "20260610_1000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_QUIESCE_SECONDS_DEFAULT = 300

# Order matters: children before parents (FKs).
_DROP_TABLES = (
    "workflow_run_steps",      # FK → workflow_runs
    "workflow_runs",
    "role_hooks",              # FK → roles/hooks
    "role_skills",             # FK → roles/skills
    "role_versions",           # FK → roles
    "hooks",
    "skills",
    "roles",
    "task_validations",
    "architect_gold_rows",
    "validator_gold_rows",
    "review_dspy_variant_pr",
    "triage_findings",
    "workflow_dispatch_dedup",
)


_TASK_STATUS_VIEW = """
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
        --    pr_merged expressions read the events table (unchanged).
        --    run.completed and step.<name>.completed expressions are
        --    re-interpreted against task_executions post-ADR-0087: the
        --    referenced task must have at least one completed execution and
        --    no running one. (workflow_run_steps no longer exists; per-step
        --    granularity collapsed into per-execution granularity.)
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
                         AND (
                             (array_length(string_to_array(d.expression, '.'), 1) = 4
                              AND split_part(d.expression, '.', 3) = 'run'
                              AND split_part(d.expression, '.', 4) = 'completed')
                             OR
                             (array_length(string_to_array(d.expression, '.'), 1) >= 5
                              AND split_part(d.expression, '.', 3) = 'step'
                              AND split_part(
                                      d.expression, '.',
                                      array_length(string_to_array(d.expression, '.'), 1)
                                  ) = 'completed')
                         )
                    THEN (
                        EXISTS (
                            SELECT 1 FROM task_executions te
                            WHERE te.task_id = split_part(d.expression, '.', 2)::uuid
                              AND te.status = 'completed'
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM task_executions te
                            WHERE te.task_id = split_part(d.expression, '.', 2)::uuid
                              AND te.status = 'running'
                        )
                    )

                    ELSE FALSE
                END
            )
        ) THEN 'blocked'

        -- 3. No task_execution: task awaiting coordinator dispatch.
        WHEN lte.id IS NULL THEN 'registered'

        -- 4. github.pr_merged is authoritative once present.
        WHEN EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.id
              AND e.entity_type = 'github'
              AND e.action = 'pr_merged'
        ) THEN 'pr_merged'

        -- 5. Most recent task_execution running.
        WHEN lte.status = 'running' THEN lte.worker_label || ': executing'

        -- 6. Most recent task_execution failed.
        WHEN lte.status = 'failed' THEN lte.worker_label || ': failed'

        -- 7. Execution completed + PR open (task_prs row, no pr_merged yet).
        WHEN EXISTS (
            SELECT 1 FROM task_prs tp
            WHERE tp.task_id = t.id
        ) THEN 'pr_opened'

        -- 8. Execution completed, no PR registered: work finished locally.
        ELSE 'done'

    END AS derived_status
FROM tasks t
LEFT JOIN LATERAL (
    SELECT te.id, te.status, te.worker_label
    FROM task_executions te
    WHERE te.task_id = t.id
    ORDER BY te.started_at DESC
    LIMIT 1
) lte ON true;
"""


_TASK_MERGEABILITY_VIEW = """
CREATE VIEW task_mergeability AS
SELECT
    t.id AS task_id,
    tp.repo,
    tp.pr_number,
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


def _quiesce_seconds() -> int:
    raw = os.environ.get("DEPRECATED_TABLE_QUIESCE_SECONDS", "")
    try:
        return int(raw) if raw else _QUIESCE_SECONDS_DEFAULT
    except ValueError:
        return _QUIESCE_SECONDS_DEFAULT


def upgrade() -> None:
    conn = op.get_bind()

    # ── Precondition guard (ADR-0087 Phase 4) ────────────────────────
    # LOCK ... NOWAIT fails immediately if an active coordinator holds
    # any transaction touching workflow_runs — closing the race between
    # our MAX(created_at) check and an in-flight INSERT. The lock is
    # released when the migration transaction commits or aborts.
    try:
        conn.execute(sa.text(
            "LOCK TABLE workflow_runs IN EXCLUSIVE MODE NOWAIT"
        ))
    except Exception as exc:  # OperationalError: lock not available
        raise RuntimeError(
            "could not acquire exclusive lock on workflow_runs — an "
            "active writer (live coordinator session?) holds a "
            "transaction. Restart coordinator-<slug> sessions so they "
            "pick up the ADR-0087 CLAUDE.md (which writes "
            "task_executions, not workflow_runs), then retry."
        ) from exc

    quiesce = _quiesce_seconds()
    recent = conn.execute(sa.text(
        "SELECT MAX(created_at) >= NOW() - make_interval(secs => :q) "
        "FROM workflow_runs"
    ), {"q": quiesce}).scalar()
    if recent:
        raise RuntimeError(
            f"workflow_runs received an INSERT within the last "
            f"{quiesce}s — live coordinator detected. Restart "
            "coordinator-<slug> sessions then retry the migration. "
            "(Window configurable via DEPRECATED_TABLE_QUIESCE_SECONDS.)"
        )

    # ── VIEWs first (they reference the tables being dropped) ────────
    op.execute("DROP VIEW IF EXISTS task_status")
    op.execute("DROP VIEW IF EXISTS task_mergeability")

    # ── Drop the fifteen legacy tables, children before parents ──────
    for table in _DROP_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')

    # ── Recreate the VIEWs against the ADR-0087 surface ──────────────
    op.execute(_TASK_STATUS_VIEW.strip())
    op.execute(_TASK_MERGEABILITY_VIEW.strip())


def downgrade() -> None:
    raise NotImplementedError(
        "ADR-0087 Phase 4 table drops are not reversible by migration — "
        "the dropped tables carried run history + DSPy corpora that an "
        "empty re-CREATE would silently lose. Restore from a database "
        "backup instead."
    )
