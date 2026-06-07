"""Typed event payloads.

Per ADR-0011, every read/write of the polymorphic ``events.payload`` JSONB
column goes through one of the per-event-type Pydantic models registered
here. Raw ``dict[str, Any]`` access of the column is forbidden by reviewer
convention; the registry + ``parse_payload`` / ``encode_payload`` helpers
are the single seam between JSONB storage and typed application code.
"""

from treadmill_api.events.architect_verdict import ArchitectVerdict
from treadmill_api.events.base import EventPayload
from treadmill_api.events.crystallization_verdict import CrystallizationVerdict
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
    StepSkipped,
    StepStarted,
)
from treadmill_api.events.step_output import (
    Artifact,
    Metadata,
    StepOutput,
)
from treadmill_api.events.review import ReviewOverride
from treadmill_api.events.rule_corpus_audit import RuleCorpusAudit, RuleCorpusAuditEntry
from treadmill_api.events.schedule import ScheduledTick
from treadmill_api.events.task import OperatorHintSet, TaskAutoMerged, TaskCancelled, TaskEscalatedToOperator, TaskEscalationAcknowledged, TaskEscalationClosed, TaskReady, TaskRegistered, TaskRetry, TaskWorkerDepsFailed, TaskWorkerHintRequested
from treadmill_api.events.validate import ValidateOverride
from treadmill_api.events.validator_tuning import ValidatorTuning


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
    "CrystallizationVerdict",
    # Step output envelope (ADR-0012)
    "StepOutput",
    "Artifact",
    "Metadata",
    # Task events
    "TaskAutoMerged",
    "TaskCancelled",
    "TaskEscalatedToOperator",
    "TaskEscalationAcknowledged",
    "TaskEscalationClosed",
    "TaskReady",
    "TaskRegistered",
    "TaskRetry",
    "TaskWorkerDepsFailed",
    "OperatorHintSet",
    "TaskWorkerHintRequested",
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
    "StepSkipped",
    # GitHub events
    "GithubPrOpened",
    "GithubPrSynchronize",
    "GithubPrMerged",
    "GithubPrReviewSubmitted",
    "GithubCheckRunCompleted",
    "GithubPrConflict",
    # Review override (ADR-0038)
    "ReviewOverride",
    # Validate override (ADR-0042)
    "ValidateOverride",
    # Internal control-plane events
    "DispatchPublishFailed",
    "DispatchPublishReplayed",
    # Scheduled-tick events (ADR-0035)
    "ScheduledTick",
    # Validator tuning proposal (ADR-0040)
    "ValidatorTuning",
    # Rule corpus audit envelope
    "RuleCorpusAudit",
    "RuleCorpusAuditEntry",
]
