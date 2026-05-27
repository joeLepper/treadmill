"""Seed the single "system" Plan that owns scheduler-spawned + operator-
triggered synthetic tasks (ADR-0057).

Per ADR-0057, every scheduled-tick dispatch and every operator-trigger
endpoint call creates a synthetic ``Task`` and uses the normal task-bound
``dispatch_task`` path. All such synthetic tasks share one parent Plan —
this module seeds it.

Two paths (mirroring ``seed/schedules.py``):
  * ``seed_system_plan_if_empty(session)`` — direct DB INSERT at API
    startup. Idempotent: when the system Plan row already exists, no-op.
  * ``seed_system_plan(api_client)`` — HTTP-driven; here only because the
    schedules seed offers the same shape. Not strictly required at v1.

The system Plan must be in derived_status ``active`` so the dispatch_task
"plan-active gate" (ADR-0010 / dispatch.py) doesn't park synthetic-task
runs in deferred-dispatch. We INSERT both the Plan row AND a
``plan.activated`` event in the same transaction.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger("treadmill.api.seed.system_plan")


# Stable UUID so callers can reference the system Plan without a runtime
# lookup. The constant is the canonical identifier; treat it as the
# moral equivalent of a hard-coded sentinel row id.
SYSTEM_PLAN_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Default repo for the system Plan. Synthetic tasks override this with the
# dispatch payload's ``repo`` (the schedule's payload_template or the
# operator-trigger payload), so this default is only ever read for plans
# that ship with no per-dispatch repo — currently none. We pick the
# dogfood repo as the safe default.
_DEFAULT_REPO = "joeLepper/treadmill"

_INTENT = (
    "System plan — parent of scheduler-spawned + operator-triggered "
    "synthetic tasks (ADR-0057). Each scheduled tick / operator trigger "
    "creates one Task under this Plan."
)


def seed_system_plan_if_empty(session: Any) -> int:
    """Insert the system Plan + its ``plan.activated`` event when absent.

    Called from the API startup path after roles/workflows seed but
    before any scheduled tick could fire. Idempotent: when the row
    already exists, returns 0 without inserting.

    Returns 1 when freshly seeded, 0 when already present.
    """
    from sqlalchemy import select as sa_select

    from treadmill_api.models.event import Event
    from treadmill_api.models.plan import Plan

    existing = session.execute(
        sa_select(Plan).where(Plan.id == SYSTEM_PLAN_ID)
    ).scalar_one_or_none()
    if existing is not None:
        logger.debug(
            "seed_system_plan_if_empty: system Plan %s already present; skipping",
            SYSTEM_PLAN_ID,
        )
        return 0

    session.add(
        Plan(
            id=SYSTEM_PLAN_ID,
            repo=_DEFAULT_REPO,
            intent=_INTENT,
            created_by="auto-seed",
            # auto_merge stays None — synthetic tasks inherit each
            # dispatch's auto-merge intent via the task layer.
        )
    )
    # Flush the Plan INSERT before adding the Event so the FK constraint
    # ``events_plan_id_fkey`` can see the new ``plans.id`` row in the
    # same transaction. Without this, SQLAlchemy's UnitOfWork can't
    # infer the insert order (the Plan's PK is passed as an explicit
    # value bypassing its ``server_default=gen_random_uuid()``, so
    # dependency analysis doesn't link the Event's ``plan_id`` to the
    # pending Plan row) — at commit time the Event INSERT can run
    # first and Postgres rejects it with a ForeignKeyViolation. Caught
    # in dev-local right after PR #40 deploy (2026-05-27).
    session.flush()
    # plan_status VIEW is last-event-wins; ``plan.activated`` lifts the
    # system Plan into ``derived_status='active'`` so dispatch_task's
    # plan-active gate lets synthetic-task runs publish + send to SQS
    # immediately (no deferred-dispatch).
    session.add(
        Event(
            entity_type="plan",
            action="activated",
            plan_id=SYSTEM_PLAN_ID,
            payload=json.loads(json.dumps({})),  # empty payload; activation has no fields
        )
    )

    session.commit()
    logger.info("seed_system_plan_if_empty: seeded system Plan %s", SYSTEM_PLAN_ID)
    return 1
