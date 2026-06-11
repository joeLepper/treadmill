"""Event emission — persist an Event row, then publish it on the bus.

Pre-ADR-0087 this module owned ``dispatch_task``: materializing a
``WorkflowRun`` + step rows and publishing ``step.ready`` to the
autoscaler's SQS work queue. That execution model is gone (Phases 4–5
dropped the tables; coordinators drive workers directly per ADR-0087).
What survives is the durable-event seam every HTTP emitter uses:

* ``Dispatcher.persist_and_publish`` — INSERT the Event row (source of
  truth), flush, then ``publisher.publish``. A publish failure is
  logged AND recorded as a ``DispatchPublishFailed`` Event-row marker
  (A.8); the ``ReplayLoop`` (A.10) re-publishes from those markers on
  its 30s tick. Persisting before publishing means a partial failure
  always leaves consistent DB state.
* ``get_dispatcher`` — the FastAPI dependency the events / escalations /
  dashboard-cancel routers use.

Pre-ADR-0087, only the ``dispatch_task`` path wrote failure markers;
``persist_and_publish`` swallowed publish errors with a log line. The
marker write now lives in ``persist_and_publish`` itself so every
emitter gets replay healing, which is what keeps the ReplayLoop
load-bearing post-Phase 5.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.eventbus import EventPublisher
from treadmill_api.events import EventPayload
from treadmill_api.events.registry import encode_payload
from treadmill_api.events.internal import DispatchPublishFailed
from treadmill_api.models import Event

logger = logging.getLogger("treadmill.dispatch")


class Dispatcher:
    """Bundles the publisher needed to emit durable events. One instance
    per request, constructed via the ``get_dispatcher`` FastAPI
    dependency (or ``from_app_state`` for background callers)."""

    def __init__(self, publisher: EventPublisher) -> None:
        self.publisher = publisher

    @classmethod
    def from_app_state(cls, state: Any) -> "Dispatcher":
        """Construct from a FastAPI ``app.state`` (or any object with the
        same attributes). Keeps background callers out of the FastAPI
        ``Request`` dependency."""
        return cls(publisher=state.publisher)

    async def persist_and_publish(
        self,
        session: AsyncSession,
        *,
        entity_type: str,
        action: str,
        payload: EventPayload,
        plan_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        commit_sha: str | None = None,
    ) -> Event:
        """INSERT an Event row, flush, and publish it on the bus.

        The Event row is the source of truth. A publish failure is
        logged and recorded as a ``DispatchPublishFailed`` marker so the
        ``ReplayLoop`` re-publishes it — callers never see the failure.

        ``commit_sha`` stamps the ADR-0014 column the mergeability VIEW
        joins on — pass it for any github.* event keyed to a head SHA
        (e.g. the lazy ``pr_conflict`` resolver, task 536bf319).
        """
        event = await self._persist_event(
            session,
            entity_type=entity_type, action=action, payload=payload,
            plan_id=plan_id, task_id=task_id, run_id=run_id, step_id=step_id,
            commit_sha=commit_sha,
        )
        try:
            await self.publisher.publish(event, payload)
        except Exception as exc:
            logger.exception(
                "failed to publish %s.%s to events bus; continuing "
                "(Event row %s persisted; replay marker written)",
                entity_type, action, event.id,
            )
            await self._record_publish_failed(
                session,
                original_event_id=event.id,
                target="sns",
                error=exc,
                plan_id=plan_id,
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
            )
        return event

    async def _persist_event(
        self,
        session: AsyncSession,
        *,
        entity_type: str,
        action: str,
        payload: EventPayload,
        plan_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        commit_sha: str | None = None,
    ) -> Event:
        """INSERT + flush an Event row. No external publish.

        ``commit_sha`` populates the per-ADR-0014 column on the Event
        row for emitters that carry one (webhook ingress).
        """
        event = Event(
            entity_type=entity_type,
            action=action,
            plan_id=plan_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            payload=encode_payload(payload),
            commit_sha=commit_sha,
        )
        session.add(event)
        await session.flush()
        return event

    async def _record_publish_failed(
        self,
        session: AsyncSession,
        *,
        original_event_id: uuid.UUID,
        target: str,
        error: BaseException,
        plan_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        run_id: uuid.UUID | None,
        step_id: uuid.UUID | None,
    ) -> None:
        """Persist a ``DispatchPublishFailed`` marker referencing the
        original event whose publish failed (A.8). Logged at WARNING
        with structured fields so ops alerting can fire on a sustained
        replay backlog. Errors emitting the marker itself are swallowed —
        a write storm against ``events`` should not crash the request."""
        payload = DispatchPublishFailed(
            original_event_id=original_event_id,
            target=target,  # type: ignore[arg-type]
            error_message=str(error)[:1024],
            attempted_at=datetime.now(timezone.utc),
        )
        logger.warning(
            "dispatch publish failed: target=%s original_event_id=%s error=%s",
            target, original_event_id, error,
            extra={
                "dispatch_publish_failed": True,
                "target": target,
                "original_event_id": str(original_event_id),
                "task_id": str(task_id) if task_id else None,
                "run_id": str(run_id) if run_id else None,
            },
        )
        try:
            await self._persist_event(
                session,
                entity_type="_internal",
                action="dispatch_publish_failed",
                payload=payload,
                plan_id=plan_id,
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
            )
        except Exception:
            # We can't recursively mark a failure-to-record-failure. Log
            # and move on — the original publish error is already in the
            # logs above.
            logger.exception(
                "failed to persist DispatchPublishFailed marker for "
                "original_event_id=%s; replay loop will not see this failure",
                original_event_id,
            )


def get_dispatcher(request: Request) -> Dispatcher:
    """FastAPI dependency: returns the request-scoped dispatcher.

    Reads the publisher off ``app.state`` (wired by the lifespan
    handler).
    """
    return Dispatcher.from_app_state(request.app.state)
