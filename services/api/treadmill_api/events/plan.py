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
