"""Event-type registry + parse / encode helpers.

Maps every (entity_type, action) pair to its Pydantic payload class. The
registry is the single seam between the polymorphic ``events.payload``
JSONB column and typed access in application code; raw ``dict[str, Any]``
access of the column is forbidden by ADR-0011.
"""

from __future__ import annotations

from typing import Any

from treadmill_api.events.base import EventPayload
from treadmill_api.events.github import (
    GithubCheckRunCompleted,
    GithubPrConflict,
    GithubPrMerged,
    GithubPrOpened,
    GithubPrReviewSubmitted,
    GithubPrSynchronize,
)
from treadmill_api.events.internal import (
    DispatchPublishFailed,
    DispatchPublishReplayed,
)
from treadmill_api.events.plan import (
    PlanAbandoned,
    PlanActivated,
    PlanCompleted,
    PlanPlanningStarted,
    PlanRegistered,
)
from treadmill_api.events.plan_doc import (
    PlanDocObservedInactive,
    PlanDocParseFailed,
)
from treadmill_api.events.step import (
    StepCancelled,
    StepCompleted,
    StepFailed,
    StepReady,
    StepSkipped,
    StepStarted,
)
from treadmill_api.events.review import ReviewOverride
from treadmill_api.events.schedule import ScheduledTick
from treadmill_api.events.task import (
    OperatorHintSet,
    TaskAutoMerged,
    TaskCancelled,
    TaskEscalatedToOperator,
    TaskEscalationAcknowledged,
    TaskEscalationClosed,
    TaskReady,
    TaskRegistered,
    TaskRetry,
    TaskWorkerDepsFailed,
    TaskWorkerHintRequested,
)
from treadmill_api.events.system import UnreferencedClosesReport
from treadmill_api.events.validate import ValidateOverride
from treadmill_api.events.validator_tuning import ValidatorTuning  # noqa: F401  re-exported


# Single registry of all known event payload classes. Keep this list
# exhaustive — every event type the API emits or consumes appears here.
_REGISTRY_CLASSES: list[type[EventPayload]] = [
    # Task events
    TaskAutoMerged,
    TaskCancelled,
    TaskEscalatedToOperator,
    TaskEscalationAcknowledged,
    TaskEscalationClosed,
    TaskReady,
    TaskRegistered,
    TaskRetry,
    TaskWorkerDepsFailed,
    OperatorHintSet,
    TaskWorkerHintRequested,
    # Plan events
    PlanRegistered,
    PlanPlanningStarted,
    PlanActivated,
    PlanCompleted,
    PlanAbandoned,
    # Plan-doc events (ADR-0021)
    PlanDocObservedInactive,
    PlanDocParseFailed,
    # Step events
    StepReady,
    StepStarted,
    StepCompleted,
    StepFailed,
    StepCancelled,
    StepSkipped,
    # GitHub events
    GithubPrOpened,
    GithubPrSynchronize,
    GithubPrMerged,
    GithubPrReviewSubmitted,
    GithubCheckRunCompleted,
    GithubPrConflict,
    # Review override (ADR-0038): architect's accept-as-is on a
    # ralph-loop deadlock flips review_decision in the mergeability VIEW.
    ReviewOverride,
    # Validate override (ADR-0042): sibling to ReviewOverride —
    # architect's accept-as-is on a validate-fail deadlock flips
    # validate_decision via the mergeability VIEW's validate LATERAL.
    ValidateOverride,
    # Internal control-plane events
    DispatchPublishFailed,
    DispatchPublishReplayed,
    # Scheduled-tick events (ADR-0035)
    ScheduledTick,
    # System-level coordination events
    UnreferencedClosesReport,
]


EVENT_REGISTRY: dict[tuple[str, str], type[EventPayload]] = {
    (cls.ENTITY_TYPE, cls.ACTION): cls for cls in _REGISTRY_CLASSES
}


class UnknownEventTypeError(KeyError):
    """Raised when an Event row's (entity_type, action) doesn't appear in
    the registry. Add the type to ``_REGISTRY_CLASSES`` to fix."""

    def __init__(self, entity_type: str, action: str) -> None:
        super().__init__(f"unknown event type: ({entity_type!r}, {action!r})")
        self.entity_type = entity_type
        self.action = action


def parse_payload(
    entity_type: str, action: str, payload: dict[str, Any] | None
) -> EventPayload:
    """Validate a raw payload dict against its registered Pydantic model.

    Raises ``UnknownEventTypeError`` if the (entity_type, action) pair is
    not registered, and ``pydantic.ValidationError`` if the payload doesn't
    conform to the schema.
    """
    cls = EVENT_REGISTRY.get((entity_type, action))
    if cls is None:
        raise UnknownEventTypeError(entity_type, action)
    return cls.model_validate(payload or {})


def encode_payload(payload: EventPayload) -> dict[str, Any]:
    """Serialize a typed payload to a JSON-compatible dict for storage in
    the JSONB ``events.payload`` column. UUIDs become strings; datetimes
    become ISO-8601 strings."""
    return payload.model_dump(mode="json")
