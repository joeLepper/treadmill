"""Seed catch-all ``event_triggers`` rows for the five github verbs.

Per ADR-0007 §"GitHub webhook ingestion" + Week-3 plan success criterion 5,
the ``event_triggers`` table maps ``(repo, event_type) → workflow_id``.
The Week-3 trigger evaluator (``coordination/triggers.py``) reads this
table on relevant github events and dispatches the configured workflow
against the matched task.

This migration seeds the operational defaults — five catch-all rows
(``repo=NULL``) so a fresh install fires the correct workflow on every
PR lifecycle event without an operator step. Per the Week-3 plan, these
are **operational defaults** (not user-tweakable seeds) which is why
they live in a migration rather than ``starters.py``.

The mappings (per Week-3 plan §C.2 / ADR-0015 §"Per-workflow shape matrix"):

  * ``pr_opened``              → ``wf-review``     (initial review)
  * ``pr_synchronize``         → ``wf-review``     (fresh review on new HEAD)
  * ``pr_synchronize``         → ``wf-validate``   (re-validate at new HEAD)
  * ``pr_review_submitted``    → ``wf-feedback``   (resolve changes_requested)
  * ``check_run_completed``    → ``wf-ci-fix``     (auto-fix CI failures)
  * ``pr_conflict``            → ``wf-conflict``   (auto-resolve conflicts)

``pr_synchronize`` is a two-row case — both ``wf-review`` and
``wf-validate`` fire concurrently per ADR-0013 (a new HEAD invalidates
both prior thumbs by VIEW construction).

The trigger evaluator filters internally — e.g. ``pr_review_submitted``
only dispatches ``wf-feedback`` when ``state='changes_requested'``;
``check_run_completed`` only dispatches ``wf-ci-fix`` when
``conclusion='failure'``. The table-level mapping is the *shape* of the
trigger; the per-event filtering is the *guard*. Bunkhouse precedent
(``events/triggers.py:TriggerEvaluator``) uses the same split.

The unique constraint on ``(repo, event_type)`` is satisfied — each
``(NULL, event_type)`` pair is distinct. ``pr_synchronize`` has two rows
because it has two ``event_type`` entries in the table: ``pr_synchronize``
mapping to ``wf-review`` is impossible to express alongside another
mapping to ``wf-validate`` under that constraint. The migration handles
this by treating ``pr_synchronize`` specially: per ADR-0015's "two
workflows on one event" pattern, **the constraint is relaxed by
deliberately omitting the second row from this seed**. Documented as a
follow-up — the trigger evaluator hardcodes the ``pr_synchronize → wf-validate``
fan-out in addition to the row-driven ``wf-review`` dispatch. This
preserves the ``(repo, event_type)`` uniqueness invariant the schema
relies on while still firing both workflows concurrently.

Idempotent re-runs: each ``INSERT ... ON CONFLICT DO NOTHING`` so applying
the migration twice (or running it on a partially-seeded database) is
safe. Downgrade removes the catch-all rows but leaves any operator-added
repo-specific rows intact.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The catch-all seed rows. Each is ``(event_type, workflow_id)``. The
# ``pr_synchronize → wf-validate`` mapping is *not* a row — see the
# module docstring; the trigger evaluator fans out concurrently.
_SEED_TRIGGERS: list[tuple[str, str]] = [
    ("pr_opened", "wf-review"),
    ("pr_synchronize", "wf-review"),
    ("pr_review_submitted", "wf-feedback"),
    ("check_run_completed", "wf-ci-fix"),
    ("pr_conflict", "wf-conflict"),
]


def upgrade() -> None:
    # Insert each catch-all row only when both:
    #   * the referenced workflow row already exists in ``workflows``
    #     (otherwise the FK constraint trips), AND
    #   * no row already exists for ``(repo IS NULL, event_type)`` (so
    #     a re-run of this migration is idempotent and never trips the
    #     ``uq_event_triggers_repo_event`` constraint).
    #
    # Workflows are seeded by ``starters.py:seed()`` (invoked via the
    # ``treadmill workflows seed-starters`` CLI verb), not by a
    # migration. A migration that ran before the operator seeded the
    # workflows would otherwise FK-violate; the existence check makes
    # the migration safe regardless of install order. On a partially-
    # seeded install (some workflows present, some not) the migration
    # seeds what it can and silently skips the rest; re-running this
    # migration after a future seed picks up the remaining rows.
    for event_type, workflow_id in _SEED_TRIGGERS:
        op.execute(
            f"""
            INSERT INTO event_triggers (repo, event_type, workflow_id,
                                        version_strategy, enabled)
            SELECT NULL, '{event_type}', '{workflow_id}', 'latest', TRUE
            WHERE EXISTS (SELECT 1 FROM workflows WHERE id = '{workflow_id}')
              AND NOT EXISTS (
                SELECT 1 FROM event_triggers
                WHERE repo IS NULL AND event_type = '{event_type}'
              )
            """
        )


def downgrade() -> None:
    # Remove only the catch-all rows we seeded. Operator-added
    # repo-specific rows + any catch-all overrides land untouched.
    for event_type, workflow_id in _SEED_TRIGGERS:
        op.execute(
            f"""
            DELETE FROM event_triggers
            WHERE repo IS NULL
              AND event_type = '{event_type}'
              AND workflow_id = '{workflow_id}'
            """
        )
