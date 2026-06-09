"""Coordination consumer — projects step-lifecycle events onto run state.

The consumer long-polls the events SQS queue (subscribed to the events
SNS topic in CDK) and advances ``workflow_run_steps.status`` as events
arrive. Per ADR-0011 this is the *only* writer of step status; HTTP
routes never mutate it.

Phase-2 minimum handlers:
  * ``step.started`` → status ``running`` + ``started_at``
  * ``step.completed`` → status ``completed`` + ``completed_at`` + ``output``
  * ``step.failed`` → status ``failed`` + ``completed_at`` + ``error``
  * ``step.cancelled`` → status ``cancelled``

Phase-3 additions (2026-05-11 closure plan A.11 / B.8 / D.6 / D.8):
  * ``_run`` uses exponential backoff (``1, 2, 4, 8, 16, 30, 30, ...``)
    on consecutive poll failures and reports a granular health state
    via ``status_for_health()`` (A.11).
  * ``step.completed`` with an author-shape ``pr_number`` writes a
    ``task_prs`` row and immediately calls ``drain_pending_events`` to
    resolve any GitHub-webhook Events buffered against that PR (B.8 + D.8).
  * ``step.completed`` and ``plan.activated`` fire a re-evaluation pass
    that re-scans for newly-dispatchable tasks (D.6). The dispatcher
    itself owns the dependency + plan-active gates; this handler just
    discovers candidates and calls into it.

Cross-step chaining (Week-3 B.2): when ``step.completed`` or
``step.failed`` arrives and the run's workflow has a next pending
step, ``_cross_step_dispatch`` materializes its ``step.ready`` Event
row + SQS work-queue claim. Single-step workflows (e.g. ``wf-author``)
have no next step so the helper short-circuits cleanly. Per ADR-0015
§"No cancellation; no step skipping", a failed step still triggers
the next step — the action role sees the prior decision and emits its
own no-op.

Idempotency contract
--------------------

Every status transition is guarded by a WHERE clause on the prior status,
so re-delivery of any event against an already-final step is a no-op:

  * ``started``    only acts on ``status='pending'``.
  * ``completed``  only acts on ``status IN ('pending', 'running')``.
  * ``failed``     only acts on ``status IN ('pending', 'running')``.
  * ``cancelled``  only acts on ``status='pending'``.

The Event audit row is INSERTed via ``ON CONFLICT (id) DO NOTHING`` so
that side is also idempotent on the worker-supplied ``event_id``. Out-of-
order delivery (e.g. ``started`` arriving after ``completed``) leaves
the terminal state intact — the WHERE clause rejects the late transition.

Malformed payloads
------------------

Every payload is validated through ``treadmill_api.events.registry.
parse_payload`` before any status update fires. A ``ValidationError`` or
``UnknownEventTypeError`` is treated as a producer bug: the message is
logged at WARNING and dropped (poison-safe). With the uniform
``StepOutput`` envelope (ADR-0012), the inner ``output`` shape is
validated together with the outer ``StepCompleted`` body — there is no
longer a raw-dict fallback at the wire boundary; a malformed envelope
fails the parse gate same as a malformed ``StepStarted`` body.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import func, select, String, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from treadmill_api.coordination.event_projector import (
    EventProjector,
    TaskPRWritten,
)
from treadmill_api.coordination.plan_router import PlanRouter
from treadmill_api.events.registry import (
    UnknownEventTypeError,
    parse_payload,
)
from treadmill_api.observability import extract_trace_context, get_tracer
from treadmill_api.events.step import (
    StepCancelled,
    StepCompleted,
    StepFailed,
    StepSkipped,
    StepStarted,
)
from treadmill_api.models import Event, Task, TaskPR, WorkflowRun, WorkflowRunStep
from treadmill_api.webhooks.pending_events import (
    drain_pending_events,
    pr_pending_buffer_key,
)

HealthStatus = Literal["starting", "running", "degraded", "dead"]
"""Reportable consumer states.

``starting`` — instance constructed, ``start()`` not yet awaited (or
just awaited; first poll hasn't returned).
``running`` — last poll completed without raising.
``degraded`` — at least one poll has raised since the last success; the
loop is still alive and backing off.
``dead`` — ``_run`` exited via an unhandled exception, or the asyncio
task is no longer alive for any reason. Operator-visible only after a
process restart heals it (no auto-restart at v0 per the 2026-05-11
closure plan A.11).
"""

_MAX_BACKOFF_SECONDS = 30
"""Cap on the exponential backoff between failing polls."""

_FAILURES_BEFORE_ERROR_LOG = 5
"""After this many consecutive failures we escalate the log level from
``exception`` (still WARNING-equivalent visibility) to ``error`` with a
distinctive message so operators see the loop is in trouble."""

logger = logging.getLogger("treadmill.coordination")


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


class CoordinationConsumer:
    """Background task that drains the events queue and updates run state.

    Owns no clients — all dependencies are injected so tests can construct
    one against a fake SQS or a synchronous handler.

    Back-compat shim: routing helpers + entity-type handlers moved to
    :class:`PlanRouter` in Task 2A Phase 2, but existing tests reach into
    ``consumer._maybe_fire_validate_feedback`` etc. directly (read or
    monkey-patch). ``__getattr__`` + ``__setattr__`` redirect those
    accesses to ``self.router`` so the test API is unchanged. The router
    is the real implementation; the consumer just forwards.
    """

    # Names that should NOT be redirected to the router. Anything not
    # listed here that looks like a routing helper / entity-type handler
    # (``_maybe_*``, ``_handle_*``, ``_cross_step_dispatch``,
    # ``_reevaluate``, ``_sweep_*``, ``_try_task_prs_*``,
    # ``_set_task_prs_*``, ``_update_triage_*``,
    # ``_write_task_prs_on_completed``) gets delegated.
    _LOCAL_ATTRS: frozenset[str] = frozenset({
        "sqs", "queue_url", "sessionmaker", "wait_time_seconds",
        "max_messages", "redis_client", "publisher", "dispatcher",
        "github_client", "settings", "projector", "router",
        "_stopped", "_task", "_auto_merge_task", "_health_status",
    })

    _ROUTED_PREFIXES: tuple[str, ...] = (
        "_maybe_", "_handle_", "_sweep_", "_try_task_prs_",
        "_set_task_prs_", "_update_triage_",
    )
    _ROUTED_NAMES: frozenset[str] = frozenset({
        "_cross_step_dispatch", "_reevaluate",
        "_write_task_prs_on_completed",
    })

    @classmethod
    def _is_routed(cls, name: str) -> bool:
        if name in cls._ROUTED_NAMES:
            return True
        return any(name.startswith(p) for p in cls._ROUTED_PREFIXES)

    def __getattr__(self, name: str) -> Any:
        # Only fires when normal attribute lookup fails — i.e. the name
        # isn't a class attribute, instance attribute, or method on
        # CoordinationConsumer. The routing helpers no longer live here,
        # so accessing one goes through this path.
        if self._is_routed(name):
            router = self.__dict__.get("router")
            if router is not None and hasattr(router, name):
                return getattr(router, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Redirect monkey-patches of routing helper names onto the
        # router so tests that did ``consumer._maybe_X = stub`` still
        # affect the helper actually called from route_step.
        if name not in self._LOCAL_ATTRS and self._is_routed(name):
            router = self.__dict__.get("router")
            if router is not None:
                setattr(router, name, value)
                return
        super().__setattr__(name, value)

    def __init__(
        self,
        sqs_client: Any,
        queue_url: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        wait_time_seconds: int = 10,
        max_messages: int = 10,
        redis_client: Any | None = None,
        publisher: Any | None = None,
        dispatcher: Any | None = None,
        github_client: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        self.sqs = sqs_client
        self.queue_url = queue_url
        self.sessionmaker = sessionmaker
        self.wait_time_seconds = wait_time_seconds
        self.max_messages = max_messages
        # Optional dependencies wired by the lifespan handler. They're
        # ``None`` in tests that don't exercise the corresponding behaviors:
        #   * ``redis_client`` + ``publisher`` — pending-event drain (D.8).
        #   * ``dispatcher`` — re-evaluation pass after step.completed /
        #     plan.activated (D.6).
        #   * ``github_client`` — conflict-detection sweep on pr_merged
        #     (Week 3 B.3). ``None`` skips the sweep cleanly.
        #   * ``settings`` — used by the ADR-0021 plan-merge trigger to
        #     check the per-repo allow-list. ``None`` makes the trigger
        #     short-circuit cleanly (tests that don't exercise it).
        # Each handler that uses one of these short-circuits when it's None.
        self.redis_client = redis_client
        self.publisher = publisher
        self.dispatcher = dispatcher
        self.github_client = github_client
        self.settings = settings
        # Phase-3C extraction (ADR-0011 single-writer split): the projector
        # owns every audit-row INSERT + step.status UPDATE + task_prs write.
        # The router owns every downstream routing decision (`_maybe_*` +
        # cross-step + re-evaluation + webhook drain + entity-type
        # handlers). The consumer owns the poll loop, projection commit,
        # and the transitional ``_auto_merge_loop`` background task per
        # ADR-0084 Task 2A Phase 2.
        self.projector = EventProjector()
        self.router = PlanRouter(
            sessionmaker=sessionmaker,
            projector=self.projector,
            redis_client=redis_client,
            publisher=publisher,
            dispatcher=dispatcher,
            github_client=github_client,
            settings=settings,
        )
        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        self._auto_merge_task: asyncio.Task[None] | None = None
        self._health_status: HealthStatus = "starting"

    async def start(self) -> None:
        self._stopped = False
        self._health_status = "starting"
        self._task = asyncio.create_task(self._run(), name="coordination-consumer")
        self._auto_merge_task = asyncio.create_task(
            self._auto_merge_loop(), name="auto-merge-poll",
        )
        logger.info("coordination consumer started: queue=%s", self.queue_url)

    async def stop(self) -> None:
        self._stopped = True
        for task in (self._task, self._auto_merge_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("coordination consumer raised on shutdown")
        self._task = None
        self._auto_merge_task = None
        logger.info("coordination consumer stopped")

    async def _run(self) -> None:
        """Poll loop with exponential-backoff resilience (A.11).

        Per the 2026-05-11 closure plan: each consecutive failing poll
        sleeps ``min(2 ** failures, 30)`` seconds instead of the legacy
        flat 1s. The counter resets on the first successful poll. After
        ``_FAILURES_BEFORE_ERROR_LOG`` consecutive failures we escalate
        the log message so operators see the loop is in trouble.

        No auto-restart at v0. If this coroutine exits via an unhandled
        exception, the task dies and ``status_for_health()`` flips to
        ``'dead'`` — ``/health/ready`` returns 503 and an operator
        restarts the API process. Auto-restart would hide bugs.
        """
        failures = 0
        try:
            while not self._stopped:
                try:
                    resp = await asyncio.to_thread(
                        self.sqs.receive_message,
                        QueueUrl=self.queue_url,
                        MaxNumberOfMessages=self.max_messages,
                        WaitTimeSeconds=self.wait_time_seconds,
                        MessageAttributeNames=["All"],
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failures += 1
                    if failures >= _FAILURES_BEFORE_ERROR_LOG:
                        logger.error(
                            "coordination poll loop has failed %d times in a row; "
                            "consumer is degraded — investigate SQS / network",
                            failures,
                            exc_info=True,
                        )
                    else:
                        logger.exception(
                            "coordination poll loop error (failure %d)",
                            failures,
                        )
                    self._health_status = "degraded"
                    delay = min(2 ** (failures - 1), _MAX_BACKOFF_SECONDS)
                    await asyncio.sleep(delay)
                    continue

                # Successful poll. Reset backoff + flip to running before
                # processing messages — message-handler exceptions are caught
                # inside ``_process`` and never bubble up to flip us back
                # to degraded.
                failures = 0
                self._health_status = "running"
                for message in resp.get("Messages", []):
                    await self._process(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The loop itself blew up despite the inner guard — flip to
            # ``dead`` and re-raise so the task surfaces the failure.
            self._health_status = "dead"
            raise

    async def _process(self, message: dict[str, Any]) -> None:
        try:
            record = json.loads(message["Body"])
        except (json.JSONDecodeError, KeyError):
            logger.exception("malformed sqs message; deleting to prevent poison loop")
            await self._delete(message)
            return
        trace_ctx = extract_trace_context(message.get("MessageAttributes", {}))
        tracer = get_tracer("treadmill.coordination")
        with tracer.start_as_current_span(
            "treadmill.coordination.consumer.message", context=trace_ctx,
        ):
            try:
                await self.handle(record)
            except Exception:
                logger.exception(
                    "coordination handler raised on %s.%s; leaving message for retry",
                    record.get("entity_type"),
                    record.get("action"),
                )
                return
            await self._delete(message)

    async def _delete(self, message: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self.sqs.delete_message,
            QueueUrl=self.queue_url,
            ReceiptHandle=message["ReceiptHandle"],
        )

    # ── Event dispatch ────────────────────────────────────────────────────────

    async def handle(self, record: dict[str, Any]) -> None:
        """Apply a single event record. Public so tests can drive directly.

        Payload validation through the typed registry runs before any DB
        access. A malformed payload is logged at WARNING and dropped —
        the SQS message is still deleted by the caller because re-delivery
        would just fail again.

        Architecture (post Task 2A Phase 2 split):

        * Projection writes — ``persist_event``, ``apply_step_status``,
          ``write_task_prs`` — run in this method's session and commit.
        * Routing decisions — every ``_maybe_*`` helper, cross-step
          dispatch, re-evaluation — moved to :class:`PlanRouter`. After
          the projection commits, ``handle`` delegates to
          ``self.router.route_*`` which opens its own session.

        Auto-merge race (orig lines 559-569): the pre-extraction code
        ran an explicit ``await session.flush()`` before
        ``_maybe_fire_auto_merge`` so the ``task_mergeability`` VIEW
        saw the in-flight INSERTs. The two-transaction design preserves
        the invariant without the flush — the router's session opens
        after the projection commits, so VIEW reads see committed
        state directly.
        """
        et = record.get("entity_type")
        action = record.get("action")

        if et == "plan":
            await self.router.route_plan(
                record, action=action, payload=record.get("payload") or {},
            )
            return

        if et == "github":
            await self.router.route_github(
                record, action=action, payload=record.get("payload") or {},
            )
            return

        if et == "schedule":
            await self.router.route_schedule(
                record, action=action, payload=record.get("payload") or {},
            )
            return

        if et == "task" and action == "architect_emit_failure":
            await self.router.route_task_architect_emit_failure(
                record, payload=record.get("payload") or {},
            )
            return

        if et == "task" and action in ("cancelled", "superseded"):
            await self.router.route_task_terminal_for_triage(
                record, action=action, payload=record.get("payload") or {},
            )
            return

        if et != "step":
            logger.debug("coordination ignoring %s.%s", et, action)
            return
        step_id = record.get("step_id")
        if not step_id:
            logger.warning("step event without step_id: %s.%s", et, action)
            return
        payload = record.get("payload") or {}

        # Validate the payload against its typed Pydantic model. Malformed
        # bodies / unknown event types are logged + dropped — they're
        # producer bugs, and the consumer must not pollute the audit log
        # with unparseable rows. Per decision #2, author-shape output
        # validation on ``step.completed`` is *not* in this gate — that
        # case writes the raw dict and marks completed (see _dispatch_step).
        try:
            typed = parse_payload(et, action, payload)
        except UnknownEventTypeError:
            logger.warning(
                "coordination unknown event type: %s.%s (step_id=%s)",
                et, action, step_id,
            )
            return
        except ValidationError as exc:
            logger.warning(
                "coordination dropping malformed %s.%s payload for step %s: %s",
                et, action, step_id, exc,
                extra={
                    "validation_error": exc.errors(),
                    "raw_payload": payload,
                },
            )
            return

        async with self.sessionmaker() as session:
            # Projection transaction (ADR-0011 single-writer): persist
            # the audit row, apply the step status update, write the
            # task_prs row if the step authored a PR. Commits before
            # the router takes over so the router's session reads see
            # the just-committed state — the auto-merge race rationale
            # at the old lines 559-569 is preserved without an explicit
            # flush.
            await self._persist_event(session, record, payload)
            await self._dispatch_step(session, action, step_id, typed, payload)
            await session.commit()

        # Routing transaction: every _maybe_* helper + the D.8 webhook
        # drain + _cross_step_dispatch + _reevaluate. Opens its own
        # session per PlanRouter contract.
        await self.router.route_step(
            record, typed=typed, action=action, step_id=step_id,
        )

    async def _persist_event(
        self,
        session: AsyncSession,
        record: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Backwards-compatible delegation to ``EventProjector``.

        Phase-3C left this name on the consumer so existing tests + the
        github webhook path continue to work unchanged. The body moved to
        :class:`treadmill_api.coordination.event_projector.EventProjector`
        per ADR-0011 single-writer invariant.
        """
        await self.projector.persist_audit_row(session, record, payload)

    async def _dispatch_step(
        self,
        session: AsyncSession,
        action: str | None,
        step_id: str,
        typed: Any,
        payload: dict[str, Any],
    ) -> bool:
        """Backwards-compatible delegation to ``EventProjector``."""
        return await self.projector.apply_step_status(
            session, action, step_id, typed, payload,
        )

    # ── task_prs writer + pending-events drain (B.8, D.8) ─────────────────────

    async def _write_task_prs_on_completed(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
        payload: dict[str, Any],
    ) -> None:
        """Insert a ``task_prs`` row when a step completes with a PR,
        then drain any pending webhook events buffered against that PR.

        The INSERT itself is owned by :class:`EventProjector` (single-writer
        per ADR-0011). The drain is a routing concern (it publishes new SQS
        messages that re-enter ``handle()``) and stays on the consumer —
        it'll move to ``PlanRouter`` in the Phase-3C follow-up.
        """
        written = await self.projector.write_task_prs(
            session, step_id, typed, payload,
        )
        if written is None:
            return

        # D.8 — drain any pending webhook events buffered against this
        # PR. Skips cleanly if redis_client / publisher were not wired
        # (narrow tests, log-only deployments).
        if self.redis_client is not None and self.publisher is not None:
            try:
                await drain_pending_events(
                    self.redis_client,
                    session,
                    self.publisher,
                    pr_pending_buffer_key(written.repo, written.pr_number),
                    written.task_id,
                )
            except Exception:
                logger.exception(
                    "task_prs write: drain_pending_events failed for "
                    "repo=%s pr_number=%d task_id=%s; row still committed",
                    written.repo, written.pr_number, written.task_id,
                )

    # ── Self-trigger: wf-review changes_requested → wf-feedback (#108) ────────

    async def _auto_merge_loop(self) -> None:
        """5-second tick: detect elapsed auto-merge deadlines and fire merges.

        Scans ``treadmill:auto-merge-deadline:*`` keys in Redis. For each key
        whose ``deadline_at`` has elapsed, re-verifies mergeability and issues
        ``PUT /repos/{repo}/pulls/{pr_number}/merge`` with ``merge_method=squash``.

        Short-circuits cleanly when ``redis_client`` or ``github_client`` is
        not wired. All non-cancellation exceptions are swallowed so the loop
        stays alive on transient GitHub API or Redis errors.
        """
        while not self._stopped:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            if self.redis_client is None or self.github_client is None:
                continue
            try:
                from treadmill_api.coordination.triggers import (
                    fire_elapsed_auto_merges,
                )
                fired = await fire_elapsed_auto_merges(
                    redis_client=self.redis_client,
                    sessionmaker=self.sessionmaker,
                    github_client=self.github_client,
                )
                if fired:
                    logger.info(
                        "auto-merge poll: fired %d merge(s) this tick", fired,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("auto-merge poll loop raised; continuing")

    # ── Cross-step dispatch (B.2) ─────────────────────────────────────────────

    def is_running(self) -> bool:
        """Whether the background poll task is alive.

        Used by ``CoordinationProbe`` to report consumer health on
        ``/health/ready``. ``True`` iff a task has been started AND it has
        not finished (cancelled, raised, or returned). The probe maps
        ``True`` → ``ok`` and ``False`` → ``unreachable``.
        """
        return self._task is not None and not self._task.done()

    def status_for_health(self) -> HealthStatus:
        """Granular health label for the consumer.

        One of ``starting`` / ``running`` / ``degraded`` / ``dead``. The
        ``is_running()`` boolean above is the coarser truth used by the
        ``CoordinationProbe`` — this method exposes the failure-mode
        nuance that operators need to triage. Per the 2026-05-11 closure
        plan A.11 there is no auto-restart at v0: ``dead`` is terminal
        until the API process is restarted.
        """
        # If the task itself died (raised, returned, was cancelled by
        # someone other than ``stop()``), ``is_running()`` returns False
        # and the health status must reflect that, even if ``_run`` did
        # not set it explicitly (e.g. cancellation during shutdown).
        if self._task is not None and self._task.done():
            if not self._stopped:
                return "dead"
        return self._health_status

