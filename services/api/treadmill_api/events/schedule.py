"""Scheduled-tick event payload (ADR-0035).

The scheduler emits one of these per schedule fire. entity_type="schedule",
action="tick". Consumers use schedule_id to route to the bound workflow.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from treadmill_api.events.base import EventPayload


class ScheduledTick(EventPayload):
    """Emitted by the scheduler on each cron fire.

    ``rendered_payload`` is the schedule's ``payload_template`` passed
    through as-is (variable rendering is out of scope for v0, ADR-0035).
    """

    ENTITY_TYPE: ClassVar[str] = "schedule"
    ACTION: ClassVar[str] = "tick"

    schedule_id: uuid.UUID
    workflow_id: str
    rendered_payload: dict[str, Any]
