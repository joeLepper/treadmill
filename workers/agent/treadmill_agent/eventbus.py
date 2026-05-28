"""Worker → events SNS publisher.

The worker publishes step lifecycle events directly to the events SNS
topic. The coordination consumer in the API picks them up, INSERTs
the audit Event row (idempotent on event_id), and applies the status
update — per ADR-0011 the consumer is the sole writer of step status,
so the worker never touches the DB.

Each event includes a worker-supplied ``event_id`` (UUID4) so the
consumer's INSERT-then-update path is idempotent under SQS redelivery.

Every payload is validated through the API's typed Pydantic classes
before publish (decision #1 in the Week-2 closure plan). A producer-side
``ValidationError`` is a worker bug that surfaces immediately at the
publish call — the SQS message is never created with a malformed body.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from treadmill_agent.observability import inject_trace_context
from treadmill_agent.events import (
    EventRecord,
    StepCompleted,
    StepFailed,
    StepOutput,
    StepStarted,
    StepTokenUsage,
)
from treadmill_api.events.base import EventPayload
from treadmill_api.events.task import TaskWorkerDepsFailed

logger = logging.getLogger("treadmill.agent.eventbus")


class EventPublisher:
    """SNS publisher. The boto3 SNS client is sync — workers are sync
    too, so no asyncio wrapping is needed (unlike the API publisher)."""

    def __init__(self, sns_client: Any, topic_arn: str | None) -> None:
        self.sns_client = sns_client
        self.topic_arn = topic_arn

    def publish_step_started(
        self, *, task_id: str, plan_id: str, run_id: str, step_id: str,
    ) -> None:
        payload = StepStarted(started_at=_utcnow())
        self._publish(
            entity_type="step", action="started",
            task_id=task_id, plan_id=plan_id, run_id=run_id, step_id=step_id,
            payload=payload,
        )

    def publish_step_completed(
        self,
        *,
        task_id: str, plan_id: str, run_id: str, step_id: str,
        output: StepOutput | dict[str, Any],
        token_usage: StepTokenUsage | dict[str, Any] | None = None,
    ) -> None:
        # Envelope outputs are validated as part of publish (ADR-0012). If
        # the caller passes a dict that can't be coerced to ``StepOutput``
        # the Pydantic ValidationError propagates immediately — the worker
        # crashes visibly at the publish site rather than shipping a
        # malformed payload that the consumer would have to reject later.
        if isinstance(output, StepOutput):
            validated_output = output
        else:
            validated_output = StepOutput.model_validate(output)
        # ADR-0020: per-step token counters. ``None`` for steps that
        # made no LLM call (dry-run, wf-validate). Same validate-at-publish
        # contract as ``output`` so a malformed counter dict crashes here,
        # not on the consumer side.
        validated_usage: StepTokenUsage | None
        if token_usage is None or isinstance(token_usage, StepTokenUsage):
            validated_usage = token_usage
        else:
            validated_usage = StepTokenUsage.model_validate(token_usage)
        payload = StepCompleted(
            completed_at=_utcnow(),
            output=validated_output,
            token_usage=validated_usage,
        )
        self._publish(
            entity_type="step", action="completed",
            task_id=task_id, plan_id=plan_id, run_id=run_id, step_id=step_id,
            payload=payload,
        )

    def publish_step_failed(
        self,
        *,
        task_id: str, plan_id: str, run_id: str, step_id: str,
        error: str,
    ) -> None:
        payload = StepFailed(failed_at=_utcnow(), error=error)
        self._publish(
            entity_type="step", action="failed",
            task_id=task_id, plan_id=plan_id, run_id=run_id, step_id=step_id,
            payload=payload,
        )

    def publish_task_worker_deps_failed(
        self,
        *,
        task_id: str, plan_id: str, run_id: str, step_id: str,
        repo: str,
        stage: str,
        detail: str,
        worker_deps_hash: str,
    ) -> None:
        """Emit ``task.worker_deps_failed`` (ADR-0059 Step 4).

        Fires from the runner's per-step ``repo_deps.materialize``
        seam alongside (and before) ``step.failed`` so the operator
        dashboard sees a distinct escalation signal for a registration
        failure — separate from gate-broken / architect_cap /
        stuck_task_sweep escalations. The wire envelope reuses the
        step-level IDs available at the materialize site so the
        consumer's audit trail links the event to the failing step.
        """
        payload = TaskWorkerDepsFailed(
            task_id=task_id,
            repo=repo,
            stage=stage,
            detail=detail,
            worker_deps_hash=worker_deps_hash,
        )
        self._publish(
            entity_type="task", action="worker_deps_failed",
            task_id=task_id, plan_id=plan_id, run_id=run_id, step_id=step_id,
            payload=payload,
        )

    def _publish(
        self,
        *,
        entity_type: str,
        action: str,
        task_id: str,
        plan_id: str,
        run_id: str,
        step_id: str,
        payload: EventPayload,
    ) -> None:
        # Validate the wire envelope through Pydantic too — guarantees
        # event_id is a string, payload is a dict-shaped json blob, etc.
        record = EventRecord(
            event_id=str(uuid.uuid4()),
            entity_type=entity_type,
            action=action,
            task_id=task_id,
            plan_id=plan_id,
            run_id=run_id,
            step_id=step_id,
            payload=payload.model_dump(mode="json"),
        )
        if self.sns_client is None or not self.topic_arn:
            logger.info(
                "events bus unwired; dropping %s.%s for step %s",
                entity_type, action, step_id,
            )
            return
        self.sns_client.publish(
            TopicArn=self.topic_arn,
            Message=json.dumps(record.model_dump(mode="json")),
            MessageAttributes={
                "entity_type": {"DataType": "String", "StringValue": entity_type},
                "action": {"DataType": "String", "StringValue": action},
                "task_id": {"DataType": "String", "StringValue": task_id},
                **inject_trace_context(),
            },
        )
        logger.info(
            "published %s.%s for step %s (event_id=%s)",
            entity_type, action, step_id, record.event_id,
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
