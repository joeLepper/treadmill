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
