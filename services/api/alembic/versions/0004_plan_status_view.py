"""plan_status VIEW — last-event-wins plan state per decision #13 in the
2026-05-11 closure plan.

Plans follow the state machine documented on ADR-0010:

    drafting → planning → active → completed | abandoned

The priority order required by the plan doc is:

    abandoned > completed > active > planning > drafting

The simple "last lifecycle-event wins" pattern matches this priority at
v0 because the state machine is *monotonic* except for the abandoned-
anytime transition. Concretely:

  * No events at all → ``drafting`` (default).
  * ``plan.registered`` is the only event ever emitted at drafting.
  * ``plan.planning_started`` is only emitted from drafting.
  * ``plan.activated`` is only emitted from drafting (Scenario 1) or
    planning (Scenario 2).
  * ``plan.completed`` is only emitted from active.
  * ``plan.abandoned`` may fire at any time and should win.

So the most-recent lifecycle event is always the highest-priority state
the plan has reached. The VIEW selects ``ORDER BY created_at DESC LIMIT
1`` against the lifecycle-action set, mapping each action to its derived
status. When (eventually) Treadmill needs to reach back to an earlier
state — e.g. re-opening a plan — a follow-up migration replaces this
view with an explicit-priority CASE.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PLAN_STATUS_VIEW_SQL = """
CREATE OR REPLACE VIEW plan_status AS
SELECT
    p.id,
    COALESCE(top_event.derived_status, 'drafting') AS derived_status
FROM plans p
LEFT JOIN LATERAL (
    SELECT
        CASE e.action
            WHEN 'abandoned'         THEN 'abandoned'
            WHEN 'completed'         THEN 'completed'
            WHEN 'activated'         THEN 'active'
            WHEN 'planning_started'  THEN 'planning'
            WHEN 'registered'        THEN 'drafting'
        END AS derived_status
    FROM events e
    WHERE e.plan_id = p.id
      AND e.entity_type = 'plan'
      AND e.action IN (
          'registered',
          'planning_started',
          'activated',
          'completed',
          'abandoned'
      )
    ORDER BY
        -- Explicit priority order: ``abandoned > completed > active >
        -- planning > drafting`` per ADR-0010. ``ORDER BY created_at DESC``
        -- alone is incorrect because lifecycle events emitted in the same
        -- transaction share an identical ``now()``-derived ``created_at``
        -- (Postgres ``now()`` returns the transaction start time), and
        -- the tiebreaker is arbitrary — Scenario-1 plans whose
        -- ``PlanRegistered`` and ``PlanActivated`` fire in the same txn
        -- would resolve to ``drafting`` half the time. Priority-first
        -- order is monotonically correct for the state machine and
        -- robust to timestamp ties.
        CASE e.action
            WHEN 'abandoned'         THEN 5
            WHEN 'completed'         THEN 4
            WHEN 'activated'         THEN 3
            WHEN 'planning_started'  THEN 2
            WHEN 'registered'        THEN 1
        END DESC,
        e.created_at DESC
    LIMIT 1
) top_event ON true;
"""


def upgrade() -> None:
    op.execute(_PLAN_STATUS_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS plan_status;")
