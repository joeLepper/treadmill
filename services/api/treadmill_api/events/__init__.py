"""Typed event payloads.

Per ADR-0011, every read/write of the polymorphic ``events.payload`` JSONB
column goes through one of the per-event-type Pydantic models registered
here. Raw ``dict[str, Any]`` access of the column is forbidden by reviewer
convention; the registry + ``parse_payload`` / ``encode_payload`` helpers
are the single seam between JSONB storage and typed application code.
"""

from treadmill_api.events.architect_verdict import ArchitectVerdict
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
from treadmill_api.events.registry import (
    EVENT_REGISTRY,
    UnknownEventTypeError,
    encode_payload,
    parse_payload,
)
from treadmill_api.events.step import (
    StepCancelled,
    StepCompleted,
    StepFailed,
    StepReady,
    StepStarted,
)
from treadmill_api.events.step_output import (
    Artifact,
    Metadata,
    StepOutput,
)
from treadmill_api.events.task import TaskCancelled, TaskReady, TaskRegistered


__all__ = [
    # Base
    "EventPayload",
    # Registry / helpers
    "EVENT_REGISTRY",
    "UnknownEventTypeError",
    "encode_payload",
    "parse_payload",
    # Verdict envelopes (ADR-0027, ADR-0032)
    "ArchitectVerdict",
    # Step output envelope (ADR-0012)
    "StepOutput",
    "Artifact",
    "Metadata",
    # Task events
    "TaskRegistered",
    "TaskReady",
    "TaskCancelled",
    # Plan events
    "PlanRegistered",
    "PlanPlanningStarted",
    "PlanActivated",
    "PlanCompleted",
    "PlanAbandoned",
    # Plan-doc events (ADR-0021)
    "PlanDocObservedInactive",
    "PlanDocParseFailed",
    # Step events
    "StepReady",
    "StepStarted",
    "StepCompleted",
    "StepFailed",
    "StepCancelled",
    # GitHub events
    "GithubPrOpened",
    "GithubPrSynchronize",
    "GithubPrMerged",
    "GithubPrReviewSubmitted",
    "GithubCheckRunCompleted",
    "GithubPrConflict",
    # Internal control-plane events
    "DispatchPublishFailed",
    "DispatchPublishReplayed",
]
