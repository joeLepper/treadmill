"""System-level events emitted by the coordination layer."""

from __future__ import annotations

import uuid
from typing import ClassVar

from pydantic import BaseModel

from treadmill_api.events.base import EventPayload


class UnreferencedClose(BaseModel):
    """One unreferenced escalation close (expected_followup is null/empty)."""

    task_id: str
    close_reason: str
    mttr_seconds: int
    closed_at: str


class UnreferencedClosesReport(EventPayload):
    """Report of escalation closes without expected_followup (ADR-????).

    Emitted weekly by the ``unreferenced_close_report`` sweep, covering the
    past 7 days of ``escalation_closed`` events with ``expected_followup``
    null or empty. Grouped by repo — one report event per repo found.

    The ``NotificationFanout`` (ADR-0062) is the primary consumer, emitting
    operator alerts for each report so the team can decide whether the closes
    were expected or represent a gap in documentation / followup planning.
    """

    ENTITY_TYPE: ClassVar[str] = "system"
    ACTION: ClassVar[str] = "unreferenced_closes_report"

    repo: str
    """The repository these closes belong to."""

    closes: list[UnreferencedClose]
    """List of unreferenced closes in this repo over the past 7 days."""

    window_end: str
    """ISO datetime of the end of the reporting window (when the sweep ran)."""


class SystemAutoSeededStarters(EventPayload):
    """Emitted when ``POST /api/v1/plans`` discovers an empty workflows
    table and auto-runs ``seed_starters_if_empty`` to populate it
    before persisting the plan.

    Post-mortem surprise A of the combined ADR-0085+0086 plan
    (2026-06-09): a fresh DB has an empty ``workflows`` table; the
    first ``treadmill plan submit`` 400s with "workflow 'wf-author'
    not registered" until ``treadmill workflows seed-starters`` runs
    manually. The plan-submit handler now auto-seeds on this branch
    (idempotent — same ``SELECT FOR UPDATE`` on the
    ``alembic_version`` sentinel that the startup-side
    ``_auto_seed_starters`` uses), and emits one of these so the
    dashboard + treadmill-events stream carries an audit trail of
    when the cliff-edge case fired. Happy-path submits (starters
    already present) emit nothing.
    """

    ENTITY_TYPE: ClassVar[str] = "system"
    ACTION: ClassVar[str] = "auto_seeded_starters"

    roles_seeded: int
    """Count of roles inserted by the seed transaction. Always ``> 0``
    when this event fires — the handler only emits on the seeded
    branch."""

    triggered_by: str
    """Where the auto-seed branch ran. Pinned to ``"plan_submit"`` at
    introduction; future entry points (CLI, MCP, etc.) get their own
    string so dashboards can disambiguate."""
