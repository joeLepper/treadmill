"""Worker-side event schemas — thin re-exports of the API's typed payloads.

Per ADR-0011 and decision #1 in ``docs/plans/2026-05-11-week-2-closure.md``
the worker shares the API's Pydantic models via the workspace source dep
(``treadmill-api``). Importing the typed classes through this module
gives the worker a single seam to validate outbound events at publish
time so producer bugs surface at the worker (not later on the consumer
side after the message has crossed SNS).

``EventRecord`` is the wire envelope the worker ships to SNS — it wraps
the typed payload's JSON-mode ``model_dump`` so the consumer's
``parse_payload(entity_type, action, payload)`` round-trips exactly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# Re-export the API's typed payload classes so worker callsites import
# from one place. A future refactor that renames or re-homes these on the
# API side fails this import — which is the schema-drift contract.
from treadmill_api.events.step import (  # noqa: F401  (re-exported)
    StepCompleted,
    StepFailed,
    StepStarted,
    StepTokenUsage,
)
from treadmill_api.events.step_output import (  # noqa: F401  (re-exported)
    Artifact,
    Metadata,
    StepOutput,
)


class EventRecord(BaseModel):
    """The SNS wire envelope. ``payload`` is the typed payload's
    ``model_dump(mode='json')`` dict — already validated by the time it
    reaches this model."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    entity_type: str
    action: str
    task_id: str
    plan_id: str
    run_id: str
    step_id: str
    payload: dict[str, Any]


__all__ = [
    "Artifact",
    "EventRecord",
    "Metadata",
    "StepCompleted",
    "StepFailed",
    "StepOutput",
    "StepStarted",
    "StepTokenUsage",
]
