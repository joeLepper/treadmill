"""Plan lifecycle events per ADR-0010.

State machine: drafting → planning → active → completed | abandoned.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from treadmill_api.events.base import EventPayload


class PlanRegistered(EventPayload):
    """A plan was created. Status is ``drafting`` initially."""

    ENTITY_TYPE: ClassVar[str] = "plan"
    ACTION: ClassVar[str] = "registered"

    repo: str
    intent: str | None = None
    doc_path: str | None = None


class PlanPlanningStarted(EventPayload):
    """``wf-plan`` dispatched against a drafting plan; status is now
    ``planning`` until the plan-doc PR merges."""

    ENTITY_TYPE: ClassVar[str] = "plan"
    ACTION: ClassVar[str] = "planning_started"

    workflow_version_id: uuid.UUID


class PlanActivated(EventPayload):
    """Plan transitions to ``active`` — the plan doc is on main and tasks
    are being spawned. For Scenario 2 (per ADR-0010), this fires when the
    plan-doc PR merges; for Scenario 1, it fires immediately on submit."""

    ENTITY_TYPE: ClassVar[str] = "plan"
    ACTION: ClassVar[str] = "activated"

    doc_path: str | None = None


class PlanCompleted(EventPayload):
    """Plan transitions to ``completed`` — every child task reached either
    merged or cancelled."""

    ENTITY_TYPE: ClassVar[str] = "plan"
    ACTION: ClassVar[str] = "completed"


class PlanAbandoned(EventPayload):
    """Plan transitions to ``abandoned`` — explicitly closed without
    completion."""

    ENTITY_TYPE: ClassVar[str] = "plan"
    ACTION: ClassVar[str] = "abandoned"

    reason: str | None = None


class PlanSubmitted(EventPayload):
    """A plan was submitted through a repo whose ``team_configs`` row
    names a coordinator. Emitted by ``POST /plans`` (Task D of the
    ADR-0085+0086 plan) so the coordinator subscribed to the repo can
    pick the work up without polling.

    Only fires when ``team_configs`` has a row for the plan's repo;
    repos without one stay on the legacy ``PlanRegistered`` +
    ``PlanActivated`` lifecycle and don't get this event.

    Payload fields are the minimum the coordinator needs to fan out to
    workers: the plan id (for lookups), the repo (for routing), the
    coordinator label (echoed back so the coordinator can ack against
    its own identity), and the task count (so the coordinator can size
    its initial dispatch batch).
    """

    ENTITY_TYPE: ClassVar[str] = "plan"
    ACTION: ClassVar[str] = "submitted"

    repo: str
    coordinator_label: str
    task_count: int
