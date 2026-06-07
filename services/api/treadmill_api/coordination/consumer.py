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
    """

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

        Side-effects beyond status projection (per the 2026-05-11 closure):

        * ``step.completed`` with a ``payload.pr_number`` envelope field
          writes a ``task_prs`` row (B.8) and drains any pending GitHub
          webhook events buffered against that (repo, pr_number) (D.8).
        * ``step.completed`` and ``plan.activated`` trigger a
          re-evaluation pass (D.6) that dispatches every task newly
          eligible to run.
        """
        et = record.get("entity_type")
        action = record.get("action")

        if et == "plan":
            # Only ``plan.activated`` triggers a side effect here (D.6 —
            # re-evaluation pass dispatches newly-eligible tasks). Other
            # plan lifecycle events are API-origin and already persisted;
            # the coordination consumer ignores them at v0. ``github.*``
            # and ``step.failed``-driven re-eval land in Week 3 with the
            # trigger evaluator.
            if action == "activated":
                await self._handle_plan_activated(
                    record, payload=record.get("payload") or {},
                )
            else:
                logger.debug("coordination ignoring plan.%s", action)
            return

        if et == "github":
            # Two side-effects gate on github events:
            #   * Week 3 B.3 — ``pr_merged`` fires the conflict-detection
            #     sweep against the repo's open PRs.
            #   * Week 3 C.2 — every github verb may fire one or more
            #     workflows via the ``event_triggers`` evaluator.
            # Both run for every github event; the sweep short-circuits
            # cleanly on anything that isn't ``pr_merged`` and the
            # evaluator drops events with no matching trigger rows.
            await self._handle_github_event(
                record, action=action, payload=record.get("payload") or {},
            )
            return

        if et == "schedule":
            # ADR-0035: schedule.tick events dispatch the bound workflow.
            # Other schedule lifecycle events are not actionable here.
            await self._handle_schedule_event(
                record, action=action, payload=record.get("payload") or {},
            )
            return

        # ADR-0083 — relay drop when the architect fails to emit a structured verdict.
        if et == "task" and action == "architect_emit_failure":
            await self._handle_architect_emit_failure(
                record, payload=record.get("payload") or {},
            )
            return

        # ADR-0061 — project task-terminal verbs onto any triage_findings
        # row whose dispatched_plan_id matches the task's plan. Other task
        # verbs fall through to the catch-all "ignoring" log below.
        if et == "task" and action in ("cancelled", "superseded"):
            await self._handle_task_terminal_for_triage(
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
            # Persist the Event row first (idempotent on event_id) so the
            # audit log is complete per ADR-0011 even for worker-origin
            # events. Dispatcher-origin events (step.ready) hit ON CONFLICT
            # DO NOTHING — the dispatcher already persisted them.
            await self._persist_event(session, record, payload)
            await self._dispatch_step(session, action, step_id, typed, payload)
            # B.8 + D.8 — write task_prs and drain pending events when the
            # completed step authored a PR. Runs inside the same transaction
            # as the status UPDATE so a failure here rolls the projection
            # back; the SQS retry then takes another pass.
            if action == "completed":
                await self._write_task_prs_on_completed(
                    session, step_id, typed, payload,
                )
                # task #108 path 1: when a wf-review step completes
                # with decision=changes_requested, fire wf-feedback
                # directly (we no longer get a pr_review_submitted
                # webhook because the runner posts via ``gh pr
                # comment`` instead of ``gh pr review``). The helper
                # short-circuits cleanly for non-wf-review steps and
                # for verdicts other than changes_requested.
                await self._maybe_fire_review_feedback(session, step_id, typed)
            # ADR-0029: when a wf-validate step completes with decision='fail'
            # or 'error', dispatch wf-feedback directly (convergence trigger).
            if action == "completed":
                await self._maybe_fire_validate_feedback(session, step_id, typed)
            # ADR-0037: when a wf-author step completes with decision='fail',
            # dispatch wf-feedback directly (convergence trigger).
            if action == "completed":
                await self._maybe_fire_author_feedback(session, step_id, typed)
            # Sibling to ADR-0037: when a wf-author step ends as
            # ``step.failed`` (worker crashed or died before producing a
            # decision='fail' payload), also dispatch wf-feedback.
            # Captured 2026-05-18 on task ``07c71852``: silent-death
            # left the system with no automatic retry; operator nudge
            # was the only recovery path. Same dedup + cap as the
            # decision='fail' path.
            if action == "failed":
                await self._maybe_fire_author_feedback_on_step_failed(
                    session, step_id,
                )
            # ADR-0062 Step 1: when a ``step.failed`` lands on a run that
            # has no further pending steps to dispatch AND no sibling
            # ``task.escalated_to_operator`` event has fired against the
            # task within the dedup window, escalate with
            # ``reason='terminal_step_failure'``. Sibling to the
            # cap-reached / gate-broken escalators — these own their own
            # cases; this producer covers the leftover "the run ran out
            # of recovery and nobody else raised" path.
            if action == "failed":
                await self._maybe_dispatch_terminal_step_failure(
                    session, step_id,
                )
            # ADR-0038: ralph-loop deadlock arbitration. When a
            # wf-feedback step completes with ``responded-without-change``
            # while the underlying review still says ``changes_requested``,
            # dispatch ``wf-architecture-resolve`` so role-architect
            # adjudicates. The helper short-circuits cleanly for any
            # other shape.
            if action == "completed":
                await self._maybe_fire_deadlock_arbitration(
                    session, step_id, typed,
                )
            # ADR-0048 follow-on (2026-05-19): when wf-feedback's action
            # step completes with decision=fail because the author-side
            # deterministic validation rejected the worker's diff AND no
            # PR exists, dispatch wf-architecture-resolve. MUST run after
            # the deadlock helper above so PR-bearing cases stay on the
            # deadlock path; this helper picks up the no-PR no-gate-signal
            # case (4 of 6 retries in the 2026-05-19 batch). See
            # ``maybe_dispatch_architect_on_feedback_validation_fail``
            # for the ordering invariant.
            if action == "completed":
                await self._maybe_fire_feedback_validation_fail_arbitration(
                    session, step_id, typed,
                )
            # SDE-1: no-progress wf-feedback terminal on a no-PR task
            # (responded-without-change / bare fail) → architect-on-plan.
            # MUST run after the deadlock + validation-fail helpers above.
            if action == "completed":
                await self._maybe_fire_feedback_no_progress_arbitration(
                    session, step_id, typed,
                )
            # Dead-end audit (2026-05-19): when a recovery workflow
            # (wf-ci-fix / wf-conflict / wf-doc-amend) completes with
            # decision=fail and has no productive next dispatch, surface to
            # operator instead of terminating silently.
            if action == "completed":
                await self._maybe_escalate_terminal_give_up(
                    session, step_id, typed,
                )
            # ADR-0038: companion to the dispatch helper above — when an
            # architect step.completed carries
            # ``payload.dispatch.review_override``, emit the
            # ``review.override`` Event the mergeability VIEW reads as
            # ``review_decision='approved'``.
            if action == "completed":
                await self._maybe_emit_review_override(
                    session, step_id, typed,
                )
            # ADR-0042: sibling emitter to review.override — when an
            # architect step.completed carries
            # ``payload.dispatch.validate_override``, emit the
            # ``validate.override`` Event the mergeability VIEW reads as
            # ``validate_decision='pass'``. The architect emits both
            # overrides on every deadlock accept-as-is; each only takes
            # effect in the VIEW when the corresponding gate's latest
            # signal at HEAD was a fail.
            if action == "completed":
                await self._maybe_emit_validate_override(
                    session, step_id, typed,
                )
            # ADR-0040: when the architect step.completed carries
            # ``payload.validator_tuning``, dispatch ``wf-doc-amend``
            # with intent ``tune-rule-from-architect`` so the documentarian
            # can apply the proposed rule-YAML edit under operator review.
            if action == "completed":
                await self._maybe_dispatch_rule_tuning(
                    session, step_id, typed,
                )
            # ADR-0048: when the architect step.completed carries
            # ``payload.verdict='supersede'`` with a non-empty
            # ``rewritten_description``, close the parent task's PR
            # (best-effort), create a CHILD task row with the rewritten
            # description + ``parent_task_id`` pointing back to the
            # parent, and dispatch a fresh ``wf-author`` against the
            # child. Sibling to the review/validate override emitters
            # above — independent path; fires only on supersede.
            if action == "completed":
                await self._maybe_dispatch_supersede(
                    session, step_id, typed,
                )
            # ADR-0058: when the architect step.completed carries
            # ``payload.verdict='gate-broken'``, escalate the task to
            # operator with the gate's stderr captured. Independent
            # path — fires only on gate-broken. Skips the amend-cap
            # counter (the architect's verdict isn't amend, so the
            # counter naturally doesn't advance).
            if action == "completed":
                await self._maybe_dispatch_gate_broken_escalation(
                    session, step_id, typed,
                )
            # ADR-0032/ADR-0038 partnership closure: when the architect
            # verdicts ``amend``, dispatch ``wf-feedback`` against the
            # same task so the feedback role can author the
            # remediation. The architect's payload carries a routing
            # hint but historically nothing acted on it.
            if action == "completed":
                await self._maybe_fire_architect_amend_feedback(
                    session, step_id, typed,
                )
            # ADR-0031: when a wf-validate or wf-review step completes,
            # set/push the auto-merge cooling-off deadline in Redis. The
            # 5s poll loop (``_auto_merge_loop``) fires the merge when
            # the 30s window elapses without a reset.
            #
            # Flush before the read: ``_maybe_emit_review_override`` /
            # ``_maybe_emit_validate_override`` / wf-review's own verdict
            # write may have INSERTed into ``events`` earlier in this
            # same transaction. ``_maybe_fire_auto_merge`` consults the
            # ``task_mergeability`` VIEW which projects from those rows.
            # Without an explicit flush, the VIEW reads the pre-write
            # snapshot and bails out silently. Observed 2026-05-17 on
            # PRs #132 + #133 (validate.override race), and again on
            # #136/#137/#138 (review.verdict race) — same shape, same
            # fix. See task #135 / docs/learnings/2026-05-17-auto-merge-
            # trigger-loses-race-with-validate-override.md.
            if action == "completed":
                await session.flush()
                await self._maybe_fire_auto_merge(session, step_id)
            # B.2 — cross-step dispatch. When a step terminates and the
            # workflow has a next pending step, materialize its
            # ``step.ready`` Event row + SQS claim. Per ADR-0015 §"No
            # cancellation; no step skipping", the next step runs on
            # both ``completed`` and ``failed`` terminations; the action
            # role inspects the prior step's decision and emits its own
            # no-op when appropriate. Runs inside the projection
            # transaction so the next-step Event row commits atomically
            # with the prior step's terminal status.
            if action in ("completed", "failed"):
                await self._cross_step_dispatch(session, step_id)
            await session.commit()

        # D.6 — re-evaluation pass. Outside the projection transaction
        # so a failure here doesn't undo the status projection; the
        # next event delivery will try again.
        if action == "completed":
            await self._reevaluate()

    async def _handle_plan_activated(
        self,
        record: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Handle a ``plan.activated`` event — fire the re-evaluation pass.

        The consumer doesn't project plan status (the ``plan_status``
        VIEW does), so this handler does *not* update any row. It
        validates the payload (so a malformed publisher is logged + dropped
        before opening a session) and then runs the re-evaluation pass,
        which dispatches every task in the now-active plan that has no
        unmet dependencies.
        """
        try:
            parse_payload("plan", "activated", payload)
        except (UnknownEventTypeError, ValidationError) as exc:
            logger.warning(
                "coordination dropping malformed plan.activated payload: %s", exc,
            )
            return

        # D.6 — run the re-evaluation pass. The dispatcher itself gates
        # on plan-active + dependencies, so a freshly-registered task on
        # a freshly-activated plan dispatches here.
        await self._reevaluate()

    async def _handle_schedule_event(
        self,
        record: dict[str, Any],
        *,
        action: str | None,
        payload: dict[str, Any],
    ) -> None:
        """Handle a ``schedule.*`` event — dispatch the bound workflow on tick.

        Only ``schedule.tick`` is actionable; other schedule lifecycle events
        (pause, resume, delete) are not produced to the consumer queue. An
        unknown action is ignored defensively.

        Payload validation through the typed registry fires before any DB
        access — poison-safe, same as the github and step paths. When the
        dispatcher is not wired the tick is logged at WARNING and dropped;
        the scheduler will re-fire on the next cron cycle.
        """
        if action != "tick":
            logger.debug("coordination ignoring schedule.%s", action)
            return

        try:
            typed = parse_payload("schedule", "tick", payload)
        except (UnknownEventTypeError, ValidationError) as exc:
            logger.warning(
                "coordination dropping malformed schedule.tick payload: %s", exc,
            )
            return

        if self.dispatcher is None:
            logger.warning(
                "scheduled-tick: dispatcher not wired; cannot dispatch "
                "workflow for schedule %s",
                payload.get("schedule_id"),
            )
            return

        from treadmill_api.coordination.triggers import handle_scheduled_tick
        from treadmill_api.events.schedule import ScheduledTick

        assert isinstance(typed, ScheduledTick)

        async with self.sessionmaker() as session:
            try:
                run_id = await handle_scheduled_tick(
                    session, self.dispatcher, typed=typed,
                )
            except Exception:
                logger.exception(
                    "scheduled-tick: dispatch raised for schedule %s; "
                    "swallowing so the consumer loop keeps draining",
                    typed.schedule_id,
                )
                return
            await session.commit()

        if run_id is not None:
            logger.info(
                "scheduled-tick: dispatched %s for schedule %s (run %s)",
                typed.workflow_id, typed.schedule_id, run_id,
            )

    async def _handle_github_event(
        self,
        record: dict[str, Any],
        *,
        action: str | None,
        payload: dict[str, Any],
    ) -> None:
        """Handle a github.* event — persist + fire triggers + (for
        ``pr_merged``) sweep for conflicts.

        Two side-effects, both gated on payload validation:

        * **Week 3 C.2 trigger evaluator.** Every github verb may fire
          one or more workflows via the ``event_triggers`` table. We
          look up matching rows, apply per-event filters
          (``check_run_completed.conclusion``,
          ``pr_review_submitted.state``), enforce cap policies
          (``wf-ci-fix`` / ``wf-conflict`` at 3 attempts each), and
          create a fresh ``WorkflowRun`` for each matched workflow.
        * **Week 3 B.3 conflict sweep.** ``pr_merged`` specifically
          fires the conflict-detection sweep (since merging one PR can
          cause others to develop conflicts against the new base).
          Other verbs short-circuit cleanly.

        Both side-effects share one transaction so the Event row, any
        derived ``step.ready`` rows, and any emitted ``pr_conflict``
        rows commit atomically. A failure in one rolls back all of
        them; the SQS message stays on the queue for retry.

        ``action`` is the verb (``pr_opened``, ``pr_merged``, etc.),
        passed through from the caller so we don't re-parse the record.
        """
        # Validate the payload against the registry. A malformed
        # publisher is logged + dropped — same poison-safe pattern as
        # the step path.
        try:
            typed = parse_payload("github", action or "", payload)
        except (UnknownEventTypeError, ValidationError) as exc:
            logger.warning(
                "coordination dropping malformed github.%s payload: %s",
                action, exc,
            )
            return

        async with self.sessionmaker() as session:
            # Persist the audit row first. The webhook receiver may
            # have already persisted it (same event_id); ON CONFLICT
            # DO NOTHING keeps the INSERT idempotent.
            await self._persist_event(session, record, payload)

            # ── Trigger evaluator (Week 3 C.2) ───────────────────────────
            # ``self.dispatcher`` is wired by the lifespan handler; in
            # narrow tests it may be ``None`` (e.g. tests that don't
            # exercise the trigger path). Skip cleanly in that case.
            if self.dispatcher is not None:
                from treadmill_api.coordination.triggers import (
                    evaluate_triggers,
                )

                try:
                    await evaluate_triggers(
                        session, self.dispatcher,
                        event_type=action or "",
                        payload=payload,
                    )
                except Exception:
                    logger.exception(
                        "trigger evaluator raised for github.%s "
                        "(repo=%s pr=%s); swallowing so the consumer loop "
                        "keeps draining",
                        action,
                        payload.get("repo"),
                        payload.get("pr_number"),
                    )
                    # Continue to the pr_merged sweep regardless — the
                    # two side-effects are independent.

            # ── Auto-merge arming on mergeability-affecting github verbs ──
            # (ADR-0031 arming-coverage gap; see
            # docs/plans/2026-06-05-accept-as-is-open-pr-not-terminal.md M2.)
            # These verbs can flip the task into ``derived_mergeability=
            # 'mergeable'`` *after* its last ``step.completed``, which is the
            # only seam the step-based arming watches. Re-evaluate arming here
            # so a green, approved PR that becomes mergeable via CI / a clean
            # re-push / conflict-clear / late pr_opened doesn't strand. Safe:
            # the arming body only sets the deadline when the VIEW already
            # reads ``mergeable`` and the 5s poll re-verifies before merging.
            if action in (
                "pr_opened",
                "pr_synchronize",
                "check_run_completed",
                "pr_conflict",
            ):
                await self._maybe_fire_auto_merge_on_github(
                    session,
                    repo=payload.get("repo") or "",
                    pr_number=payload.get("pr_number"),
                )

            # ── Conflict sweep (Week 3 B.3, pr_merged only) ──────────────
            if action == "pr_merged":
                await self._sweep_after_pr_merged(session, typed)
                # Task #124 — branch-name fallback for operator-completed PRs.
                # Runs after the sweep so conflicts are resolved first.
                await self._try_task_prs_fallback_on_pr_merged(session, typed)
                # Set task_prs.closed_at when a PR merges.
                await self._set_task_prs_closed_at_on_pr_merged(session, typed)
                # ADR-0061 — project the merge onto any triage_findings
                # row whose dispatched_plan_id matches this PR's task's
                # plan. Runs after the task_prs fallback so an operator-
                # completed PR that only just got its task_prs row still
                # projects on the same delivery.
                await self._update_triage_outcome_on_pr_merged(
                    session, record, typed,
                )

            await session.commit()

        # ── Plan-merge trigger (ADR-0021, pr_merged only) ────────────────
        # Runs *after* the github-event audit + sweep transaction commits
        # so the plan-doc handler opens its own session(s) for the per-doc
        # work it does. Per ADR-0021 the trigger handler is a different
        # path from ``event_triggers``; it directly creates a Plan + spawns
        # tasks from the parsed doc.
        if action == "pr_merged":
            await self._handle_plan_doc_merged(typed)
            # D.6 (extended 2026-05-13). A ``pr_merged`` event may have
            # just satisfied a dependent task's ``task.<uuid>.pr_merged``
            # expression — re-evaluate so dependent tasks dispatch
            # automatically. The redispatch module's docstring deferred
            # this from v0 ("Week 3 with the trigger evaluator"); the
            # ADR-0023 smoke surfaced the gap live (PR #20 merged but
            # tasks 2-4 of the plan never dispatched because no one
            # called reevaluate on the merge event). See
            # docs/handoffs/2026-05-13-adr-0023-smoke-and-validation-holes.md.
            await self._reevaluate()

    async def _sweep_after_pr_merged(
        self,
        session: AsyncSession,
        typed: Any,
    ) -> None:
        """Run the conflict-detection sweep after a ``pr_merged`` event.

        Extracted from the github-event entry so the trigger evaluator
        and the sweep are clearly separated. Short-circuits when the
        github client or publisher isn't wired (narrow tests, missing
        token at boot). Per Week-3 B.3 the sweep is fail-soft against
        GitHub API flakiness; a transient HTTP error logs + degrades
        to no-emit rather than crashing the consumer loop.
        """
        if self.github_client is None:
            logger.debug(
                "coordination skipping conflict sweep for github.pr_merged "
                "(repo=%s pr=%s): github_client not wired",
                typed.repo, typed.pr_number,
            )
            return
        if self.publisher is None:
            logger.warning(
                "coordination skipping conflict sweep for github.pr_merged "
                "(repo=%s pr=%s): publisher not wired",
                typed.repo, typed.pr_number,
            )
            return

        # Import inline so the consumer module doesn't unconditionally
        # pull in the sweep module's logger / dependencies in test paths
        # that never exercise the sweep.
        from treadmill_api.coordination.conflict_sweep import (
            sweep_open_prs_for_conflicts,
        )

        try:
            emitted = await sweep_open_prs_for_conflicts(
                session=session,
                publisher=self.publisher,
                github_client=self.github_client,
                repo=typed.repo,
            )
        except Exception:
            logger.exception(
                "conflict sweep raised for repo=%s pr=%s; "
                "swallowing so the consumer loop keeps draining",
                typed.repo, typed.pr_number,
            )
            return
        if emitted:
            logger.info(
                "conflict sweep emitted %d pr_conflict event(s) for "
                "repo=%s (after pr=%s merged)",
                emitted, typed.repo, typed.pr_number,
            )

    async def _try_task_prs_fallback_on_pr_merged(
        self,
        session: AsyncSession,
        typed: Any,
    ) -> None:
        """Try to populate task_prs via branch-name parsing when the normal
        path didn't find a row.

        Per task #124: if a pr_merged event has no task_prs row, parse the
        head branch name as ``task/<8-char-task-id-prefix>-<slug>``. If the
        prefix matches exactly one task, insert the task_prs row and drain
        any pending events buffered against the (repo, pr_number) pair.

        Only runs when a task_prs row doesn't already exist (operator-
        completed PRs that were authored outside of the Treadmill workflow).
        """
        # Check if task_prs already exists for this (repo, pr_number).
        result = await session.execute(
            select(TaskPR.task_id).where(
                func.lower(TaskPR.repo) == func.lower(typed.repo),
                TaskPR.pr_number == typed.pr_number,
            )
        )
        if result.scalar_one_or_none() is not None:
            # Row already exists; nothing to do.
            return

        # No head_branch in the event payload — can't parse.
        if not typed.head_branch:
            logger.debug(
                "task_prs fallback: no head_branch for repo=%s pr=%d; skipping",
                typed.repo, typed.pr_number,
            )
            return

        # Parse branch name: task/<prefix>-<slug>
        parts = typed.head_branch.split("/", 1)
        if len(parts) != 2 or parts[0] != "task":
            logger.debug(
                "task_prs fallback: head_branch=%s doesn't match "
                "task/<prefix>-<slug>; skipping",
                typed.head_branch,
            )
            return

        prefix_and_slug = parts[1]
        prefix_parts = prefix_and_slug.split("-", 1)
        # ADR-0033 requires ``task/<prefix>-<slug>`` — both halves present
        # and non-empty.
        if len(prefix_parts) != 2 or not prefix_parts[1]:
            logger.debug(
                "task_prs fallback: branch=%s missing slug suffix; skipping",
                typed.head_branch,
            )
            return

        prefix = prefix_parts[0]
        if len(prefix) != 8:
            logger.debug(
                "task_prs fallback: prefix=%s is not 8 chars; skipping",
                prefix,
            )
            return

        # Query for tasks whose ID starts with this prefix.
        result = await session.execute(
            select(Task.id, Task.repo)
            .where(
                func.left(func.cast(Task.id, String), 8) == prefix
            )
        )
        matching_tasks = result.all()

        if len(matching_tasks) != 1:
            logger.debug(
                "task_prs fallback: found %d task(s) matching prefix=%s "
                "for repo=%s pr=%d; need exactly 1",
                len(matching_tasks), prefix, typed.repo, typed.pr_number,
            )
            return

        task_id, stored_repo = matching_tasks[0]

        # Insert the task_prs row using the task's stored repo
        # (never the payload's claimed repo).
        stmt = (
            pg_insert(TaskPR)
            .values(
                repo=stored_repo,
                pr_number=typed.pr_number,
                task_id=task_id,
                branch=typed.head_branch,
            )
            .on_conflict_do_nothing(index_elements=["repo", "pr_number"])
        )
        await session.execute(stmt)
        logger.info(
            "task_prs fallback: inserted row for repo=%s pr=%d task_id=%s "
            "via branch=%s",
            stored_repo, typed.pr_number, task_id, typed.head_branch,
        )

        # D.8 — drain any pending webhook events buffered against this
        # PR. Skips cleanly if redis_client / publisher not wired.
        if self.redis_client is not None and self.publisher is not None:
            try:
                await drain_pending_events(
                    self.redis_client,
                    session,
                    self.publisher,
                    pr_pending_buffer_key(stored_repo, typed.pr_number),
                    task_id,
                )
            except Exception:
                logger.exception(
                    "task_prs fallback: drain_pending_events failed for "
                    "repo=%s pr_number=%d task_id=%s; row still committed",
                    stored_repo, typed.pr_number, task_id,
                )

    async def _set_task_prs_closed_at_on_pr_merged(
        self,
        session: AsyncSession,
        typed: Any,
    ) -> None:
        """Set task_prs.closed_at when a PR merges.

        Idempotent: only updates if closed_at is NULL. Matches (repo, pr_number)
        and sets closed_at to now().
        """
        stmt = (
            update(TaskPR)
            .where(
                func.lower(TaskPR.repo) == func.lower(typed.repo),
                TaskPR.pr_number == typed.pr_number,
                TaskPR.closed_at.is_(None),
            )
            .values(closed_at=func.now())
        )
        await session.execute(stmt)
        logger.info(
            "task_prs.closed_at set for repo=%s pr=%d (no-op if already set)",
            typed.repo, typed.pr_number,
        )

    async def _update_triage_outcome_on_pr_merged(
        self,
        session: AsyncSession,
        record: dict[str, Any],
        typed: Any,
    ) -> None:
        """Project ``triage_findings.outcome_state='merged'`` for any
        finding whose ``dispatched_plan_id`` matches the plan of the task
        bound to this PR (ADR-0061 §"Outcome tracking").

        Resolves the owning plan via ``task_prs → tasks`` so the helper
        composes with ``_try_task_prs_fallback_on_pr_merged`` (operator-
        completed PRs that only acquired a ``task_prs`` row on this very
        delivery still project on the same transaction).

        Idempotent on re-delivery: we read ``outcome_merged_at`` off the
        persisted Event row's ``created_at`` (stable across retries), so
        a re-projected event writes the same values.

        No-op when the PR has no task_prs row (operator-authored PRs we
        can't map back to a Treadmill task) or when the task's plan has
        no triage_findings row pointing at it (the common case — most
        PRs are not triage-dispatched).
        """
        # Resolve plan_id via the task_prs bridge. The same join the
        # plan-merge trigger and conflict sweep use; cheap on the
        # ``(repo, pr_number)`` index.
        plan_id = await session.scalar(
            select(Task.plan_id)
            .join(TaskPR, TaskPR.task_id == Task.id)
            .where(
                func.lower(TaskPR.repo) == func.lower(typed.repo),
                TaskPR.pr_number == typed.pr_number,
            )
        )
        if plan_id is None:
            return

        # Pull outcome_merged_at off the just-persisted Event row so
        # re-projection writes a stable value. NULL is acceptable — the
        # column is nullable — but in practice every well-formed record
        # carries an event_id, the audit INSERT/no-op leaves a row, and
        # this SELECT returns its created_at.
        merged_at: Any = None
        raw_event_id = record.get("event_id")
        if raw_event_id:
            try:
                event_uuid = uuid.UUID(str(raw_event_id))
            except (ValueError, TypeError):
                event_uuid = None
            if event_uuid is not None:
                merged_at = await session.scalar(
                    select(Event.created_at).where(Event.id == event_uuid)
                )

        from treadmill_api.triage_store import TriageStore

        rowcount = await TriageStore().update_outcome(
            session,
            dispatched_plan_id=plan_id,
            outcome_state="merged",
            outcome_pr_number=typed.pr_number,
            outcome_merged_at=merged_at,
        )
        if rowcount:
            logger.info(
                "triage outcome projected: plan_id=%s pr=%d (rows=%d)",
                plan_id, typed.pr_number, rowcount,
            )

    async def _handle_architect_emit_failure(
        self,
        record: dict[str, Any],
        *,
        payload: dict[str, Any],
    ) -> None:
        """Drop a cc-relay notification when the architect fails to emit a
        structured verdict (ADR-0083).

        The event is already persisted by the router before the consumer sees
        it; this handler fires the relay-drop side-effect so the dispatching
        orchestrator session learns about the failure without polling.
        """
        from treadmill_api.coordination.triggers import (
            maybe_drop_relay_on_architect_emit_failure,
        )
        from treadmill_api.events.task import ArchitectEmitFailure
        from pydantic import ValidationError

        try:
            typed = ArchitectEmitFailure.model_validate(payload)
        except ValidationError as exc:
            logger.warning(
                "coordination dropping malformed task.architect_emit_failure payload: %s",
                exc,
            )
            return

        task_id_str = str(record.get("task_id") or "")
        maybe_drop_relay_on_architect_emit_failure(typed, task_id_str)

    async def _handle_task_terminal_for_triage(
        self,
        record: dict[str, Any],
        *,
        action: str | None,
        payload: dict[str, Any],
    ) -> None:
        """Project ``triage_findings.outcome_state`` for terminal task
        verbs (ADR-0061 §"Outcome tracking").

        Handles ``task.cancelled`` (operator cancel) and
        ``task.superseded`` (architect supersede). The audit Event row
        commits in the same transaction as the outcome UPDATE so no
        sweeper / race window is needed. Malformed payloads are dropped
        before any DB access (same poison-safe pattern as the github /
        step paths).
        """
        try:
            parse_payload("task", action or "", payload)
        except (UnknownEventTypeError, ValidationError) as exc:
            logger.warning(
                "coordination dropping malformed task.%s payload: %s",
                action, exc,
            )
            return

        task_id = _uuid_or_none(record.get("task_id"))
        if task_id is None:
            logger.warning(
                "task.%s without task_id; skipping triage projection", action,
            )
            return

        async with self.sessionmaker() as session:
            await self._persist_event(session, record, payload)

            plan_id = await session.scalar(
                select(Task.plan_id).where(Task.id == task_id)
            )
            if plan_id is not None:
                from treadmill_api.triage_store import TriageStore

                rowcount = await TriageStore().update_outcome(
                    session,
                    dispatched_plan_id=plan_id,
                    outcome_state=action or "",
                    outcome_pr_number=None,
                    outcome_merged_at=None,
                )
                if rowcount:
                    logger.info(
                        "triage outcome projected: plan_id=%s state=%s "
                        "(rows=%d)",
                        plan_id, action, rowcount,
                    )

            await session.commit()

    async def _handle_plan_doc_merged(self, typed: Any) -> None:
        """Run the ADR-0021 plan-merge trigger after a ``pr_merged`` event.

        Bridges the consumer's per-event projection to the dedicated
        plan-doc handler. Short-circuits cleanly when its dependencies
        (``github_client``, ``dispatcher``, ``settings``) aren't wired —
        narrow tests, log-only deployments, or missing GITHUB_TOKEN at
        boot. All exceptions are caught and logged so the consumer loop
        keeps draining; the SQS message has already been processed by
        the time we get here so we never want a plan-doc failure to
        cause a retry of the surrounding github audit work.
        """
        if self.github_client is None:
            logger.debug(
                "plan-merge trigger: github_client unwired; skipping "
                "(repo=%s pr=%s)",
                typed.repo, typed.pr_number,
            )
            return
        if self.dispatcher is None:
            logger.debug(
                "plan-merge trigger: dispatcher unwired; skipping "
                "(repo=%s pr=%s)",
                typed.repo, typed.pr_number,
            )
            return
        if self.settings is None:
            logger.warning(
                "plan-merge trigger: settings unwired; skipping "
                "(repo=%s pr=%s) — plan-merge dispatch requires the "
                "per-repo allow-list",
                typed.repo, typed.pr_number,
            )
            return

        from treadmill_api.coordination.plan_doc_trigger import (
            handle_pr_merged,
        )

        try:
            await handle_pr_merged(
                sessionmaker=self.sessionmaker,
                dispatcher=self.dispatcher,
                github_client=self.github_client,
                settings=self.settings,
                repo=typed.repo,
                pr_number=typed.pr_number,
                merge_commit_sha=getattr(typed, "merged_sha", None),
                sender=getattr(typed, "sender", None),
            )
        except Exception:
            logger.exception(
                "plan-merge trigger raised for repo=%s pr=%s; "
                "swallowing so the consumer loop keeps draining",
                typed.repo, typed.pr_number,
            )

    async def _persist_event(
        self,
        session: AsyncSession,
        record: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """INSERT the Event row idempotently. Pre-existing (event_id)
        rows — typically dispatcher-origin events — are left untouched."""
        raw_id = record.get("event_id")
        if not raw_id:
            # No id supplied (older publishers) — skip persistence; the
            # Event row will not be written but the status update still
            # applies. Worker publishers should always supply event_id.
            logger.debug(
                "event without event_id; skipping audit INSERT (%s.%s)",
                record.get("entity_type"), record.get("action"),
            )
            return
        try:
            event_id = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            logger.warning("malformed event_id in record: %r", raw_id)
            return

        stmt = (
            pg_insert(Event)
            .values(
                id=event_id,
                entity_type=record.get("entity_type"),
                action=record.get("action"),
                plan_id=_uuid_or_none(record.get("plan_id")),
                task_id=_uuid_or_none(record.get("task_id")),
                run_id=_uuid_or_none(record.get("run_id")),
                step_id=_uuid_or_none(record.get("step_id")),
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.execute(stmt)

    async def _dispatch_step(
        self,
        session: AsyncSession,
        action: str | None,
        step_id: str,
        typed: Any,
        payload: dict[str, Any],
    ) -> bool:
        """Apply the validated typed step event to ``workflow_run_steps``.

        ``typed`` is the validated payload object — use it for field
        access in preference to ``payload`` raw-dict lookups. ``payload``
        is retained as a parameter for symmetry with the prior union
        shape and for the audit trail, though with the uniform envelope
        (ADR-0012) every ``completed`` event's ``output`` is already a
        fully-validated ``StepOutput``.

        Each ``UPDATE`` is gated on the prior status — the WHERE clause
        is the idempotency mechanism for re-delivery. See the module
        docstring for the full transition table.
        """
        if action == "started":
            assert isinstance(typed, StepStarted)
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status == "pending",
                )
                .values(status="running", started_at=typed.started_at)
            )
            return True
        if action == "completed":
            assert isinstance(typed, StepCompleted)
            # ``typed.output`` is a validated ``StepOutput`` envelope
            # (ADR-0012). The top-level ``parse_payload`` gate at the
            # entry to ``handle()`` already rejected malformed envelopes,
            # so we can dump the typed object directly. The JSONB column
            # holds the parser-stable shape the future consumer round-
            # trips through ``StepOutput.model_validate``.
            output_to_store = typed.output.model_dump(mode="json")
            # ADR-0020: per-step token counters land in five dedicated
            # nullable columns in the same UPDATE — NULL when the worker
            # omitted ``token_usage`` (dry-run, wf-validate, or any step
            # that made no LLM call). All five fields move together
            # because the worker pairs counters with the model id; the
            # consumer never persists a partial usage row.
            usage = typed.token_usage
            values: dict[str, Any] = {
                "status": "completed",
                "completed_at": typed.completed_at,
                "output": output_to_store,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "cache_creation_tokens": (
                    usage.cache_creation_tokens if usage else None
                ),
                "cache_read_tokens": usage.cache_read_tokens if usage else None,
                "model": usage.model if usage else None,
            }
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status.in_(("pending", "running")),
                )
                .values(**values)
            )
            return True
        if action == "failed":
            assert isinstance(typed, StepFailed)
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status.in_(("pending", "running")),
                )
                .values(
                    status="failed",
                    completed_at=typed.failed_at,
                    error=typed.error,
                )
            )
            return True
        if action == "cancelled":
            assert isinstance(typed, StepCancelled)
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status == "pending",
                )
                .values(status="cancelled")
            )
            return True
        if action == "skipped":
            assert isinstance(typed, StepSkipped)
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status == "pending",
                )
                .values(status="skipped")
            )
            return True
        logger.debug("coordination ignoring step.%s", action)
        return False

    # ── task_prs writer + pending-events drain (B.8, D.8) ─────────────────────

    async def _write_task_prs_on_completed(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
        payload: dict[str, Any],
    ) -> None:
        """Insert a ``task_prs`` row when a step completes with a PR.

        Per the 2026-05-11 closure plan B.8 the coordination consumer is
        the *only* writer of ``task_prs``. Per ADR-0012's convention map
        for ``wf-author``, the envelope carries the PR reference across
        three fields:

        * ``payload["pr_number"]`` — the PR number (when the PR opened).
        * ``artifacts[kind="branch"]`` — the branch name.
        * top-level ``commit_sha`` — the commit anchor (not used here;
          ADR-0013's mergeability VIEW joins on it).

        We look up the owning task by joining ``workflow_run_steps →
        workflow_runs → tasks`` and INSERT ``(repo, pr_number, task_id,
        branch)`` with ``ON CONFLICT DO NOTHING`` for idempotency on
        re-delivery.

        Defense against payload spoofing: the ``repo`` we write is the
        *task's* stored ``tasks.repo``, never the worker-reported value.
        A compromised worker that fabricated an alien repo string would
        otherwise plant a bogus bridge row.

        After the INSERT lands, drain any pending GitHub webhook events
        that were buffered against this (repo, pr_number) pair (D.8).
        """
        # ``typed.output`` is a validated ``StepOutput`` envelope after
        # the top-level parse_payload gate. We read the per-workflow
        # convention fields (ADR-0012 §"Convention map for wf-author's
        # payload") from the envelope:
        #   * pr_number  - payload["pr_number"]  (absent when no PR opened)
        #   * branch     - first artifact of kind="branch" (worker always
        #                  emits one for wf-author per ADR-0012)
        # Workflows that do not author PRs (wf-review, wf-validate, etc.)
        # simply omit ``pr_number`` from the envelope's payload — the
        # short-circuit below returns without writing a task_prs row.
        if not isinstance(typed, StepCompleted):
            return
        envelope = typed.output
        pr_number_raw = envelope.payload.get("pr_number")
        if pr_number_raw is None:
            # Local-mode runs (no PR opened) or non-PR-authoring
            # workflows fall here. No task_prs row to write; the drain
            # has nothing to do because no webhook ever resolves against
            # this pair.
            return
        if not isinstance(pr_number_raw, int) or isinstance(pr_number_raw, bool):
            logger.warning(
                "task_prs write: payload.pr_number not an int for step %s; "
                "skipping (got %r)",
                step_id, pr_number_raw,
            )
            return
        pr_number = pr_number_raw

        branch: str | None = None
        for artifact in envelope.artifacts:
            if artifact.kind == "branch":
                branch = artifact.value
                break
        if branch is None:
            logger.warning(
                "task_prs write: no branch artifact for step %s (pr_number=%d); "
                "skipping",
                step_id, pr_number,
            )
            return

        # Resolve the (task_id, repo) for this step. We trust the task's
        # stored repo — never the payload's claim.
        result = await session.execute(
            select(WorkflowRun.task_id, Task.repo)
            .join(WorkflowRunStep, WorkflowRunStep.run_id == WorkflowRun.id)
            .join(Task, Task.id == WorkflowRun.task_id)
            .where(WorkflowRunStep.id == step_id)
        )
        row = result.first()
        if row is None:
            logger.warning(
                "task_prs write: no task found for step_id=%s; skipping", step_id,
            )
            return
        task_id = row.task_id
        repo = row.repo

        stmt = (
            pg_insert(TaskPR)
            .values(
                repo=repo,
                pr_number=pr_number,
                task_id=task_id,
                branch=branch,
            )
            .on_conflict_do_nothing(index_elements=["repo", "pr_number"])
        )
        await session.execute(stmt)
        logger.info(
            "task_prs row written: repo=%s pr_number=%d task_id=%s",
            repo, pr_number, task_id,
        )

        # D.8 — drain any pending webhook events buffered against this
        # PR. Skips cleanly if redis_client / publisher were not wired
        # (narrow tests, log-only deployments).
        if self.redis_client is not None and self.publisher is not None:
            try:
                await drain_pending_events(
                    self.redis_client,
                    session,
                    self.publisher,
                    pr_pending_buffer_key(repo, pr_number),
                    task_id,
                )
            except Exception:
                logger.exception(
                    "task_prs write: drain_pending_events failed for "
                    "repo=%s pr_number=%d task_id=%s; row still committed",
                    repo, pr_number, task_id,
                )

    # ── Self-trigger: wf-review changes_requested → wf-feedback (#108) ────────

    async def _maybe_fire_review_feedback(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """Companion side-effect to ``_write_task_prs_on_completed`` —
        when a ``wf-review.step.completed`` arrives with
        ``decision='changes_requested'``, dispatch ``wf-feedback``
        directly (path 1 of task #108 — see
        ``coordination/triggers.maybe_dispatch_feedback_on_review_changes_requested``).

        Skips cleanly when ``self.dispatcher`` is ``None`` (narrow
        tests that don't exercise dispatch). Failures are logged but
        do not propagate — the prior step's projection has already
        committed; rolling that back on a dispatch failure would lose
        progress. The dispatch helper's own retry / dedup logic
        handles transient errors on the next event delivery.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_feedback_on_review_changes_requested,
            maybe_dispatch_feedback_on_terminal_failure,
        )
        try:
            await maybe_dispatch_feedback_on_review_changes_requested(
                session, self.dispatcher, step_id=step_id, typed=typed,
            )
            # 2026-05-15: the reviewer's ``comment`` verdict was retired
            # at the source (review disposition + role prompt). Defensive
            # net: if a stale role config or test fixture somehow emits
            # ``needs-more-info`` anyway, route to wf-feedback rather
            # than letting the task stall in a black hole.
            await maybe_dispatch_feedback_on_terminal_failure(
                session, self.dispatcher,
                step_id=step_id, typed=typed,
                workflow_id="wf-review",
                fail_decision="needs-more-info",
            )
        except Exception:
            logger.exception(
                "_maybe_fire_review_feedback: dispatch failed for step %s; "
                "prior projection committed, will retry on redelivery",
                step_id,
            )

    # ── Self-trigger: ralph-loop deadlock → wf-architecture-resolve (ADR-0038)

    async def _maybe_fire_deadlock_arbitration(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """ADR-0038 ralph-loop deadlock detector.

        Fires ``wf-architecture-resolve`` against the same task when a
        ``wf-feedback.step.completed`` arrives with
        ``decision='responded-without-change'`` while the latest
        ``wf-review`` decision is ``'changes_requested'``. The helper
        short-circuits cleanly for any other shape and applies its own
        5-attempt cap + dedup.

        Failures are logged but do not propagate; the prior projection
        has already committed and rolling back on a dispatch failure
        would lose progress.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_arbitration_on_deadlock,
        )
        try:
            await maybe_dispatch_arbitration_on_deadlock(
                session, self.dispatcher, step_id=step_id, typed=typed,
            )
        except Exception:
            logger.exception(
                "_maybe_fire_deadlock_arbitration: dispatch failed for "
                "step %s; prior projection committed, will retry on "
                "redelivery",
                step_id,
            )

    # ── Self-trigger: feedback-validation-fail → wf-architecture-resolve ─
    #   (ADR-0048 follow-on, 2026-05-19)

    async def _maybe_fire_feedback_validation_fail_arbitration(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """ADR-0048 follow-on: when wf-feedback's action step completes
        with ``decision='fail'`` because author-side deterministic
        validation rejected the worker's diff (no PR was opened),
        dispatch ``wf-architecture-resolve`` so the architect arbitrates.

        Sibling to ``_maybe_fire_deadlock_arbitration``. Ordering:
        this helper MUST be invoked AFTER the deadlock helper. The
        deadlock helper fires for PR-bearing cases (a blocking gate
        signal exists for the task); the validation-fail-no-PR case
        skips deadlock cleanly (no gate signal) and is picked up
        here. Documented as an ordering invariant on the trigger
        function itself.

        Failures are logged but do not propagate; the prior projection
        has already committed and rolling back on a dispatch failure
        would lose progress.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_architect_on_feedback_validation_fail,
        )
        try:
            await maybe_dispatch_architect_on_feedback_validation_fail(
                session, self.dispatcher, step_id=step_id, typed=typed,
            )
        except Exception:
            logger.exception(
                "_maybe_fire_feedback_validation_fail_arbitration: "
                "dispatch failed for step %s; prior projection committed, "
                "will retry on redelivery",
                step_id,
            )

    async def _maybe_fire_feedback_no_progress_arbitration(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """SDE-1 (2026-05-19 dead-end audit): when wf-feedback's action step
        terminates without progress on a no-PR task (responded-without-change,
        or a bare decision=fail with no validation_results), route to
        wf-architecture-resolve. MUST run after the deadlock and
        validation-fail helpers so those claim the gate-bearing and
        validation-fail cases first.

        Failures are logged but do not propagate; the prior projection has
        already committed.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_architect_on_feedback_no_progress,
        )
        try:
            await maybe_dispatch_architect_on_feedback_no_progress(
                session, self.dispatcher, step_id=step_id, typed=typed,
            )
        except Exception:
            logger.exception(
                "_maybe_fire_feedback_no_progress_arbitration: dispatch "
                "failed for step %s; prior projection committed, will retry "
                "on redelivery",
                step_id,
            )

    # ── Operator backstop: recovery-workflow give-up (dead-end audit) ───────

    async def _maybe_escalate_terminal_give_up(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """SDE-2/4b/6 (2026-05-19 dead-end audit): when wf-ci-fix /
        wf-conflict / wf-doc-amend completes with ``decision='fail'`` and has
        no productive next dispatch, emit ``task.escalated_to_operator`` so the
        needs_operator query surfaces it. Previously these terminated silently
        (no consumer handler keyed on these workflows' own terminals).

        Failures are logged but do not propagate; the prior projection has
        already committed.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_escalate_operator_on_terminal_give_up,
        )
        try:
            await maybe_escalate_operator_on_terminal_give_up(
                session, self.dispatcher, step_id=step_id, typed=typed,
            )
        except Exception:
            logger.exception(
                "_maybe_escalate_terminal_give_up: escalation failed for "
                "step %s; prior projection committed, will retry on redelivery",
                step_id,
            )

    # ── Self-trigger: architect amend → wf-feedback (ADR-0032/0038 closure) ─

    async def _maybe_fire_architect_amend_feedback(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """Companion to the deadlock-arbitration helper: when the
        architect verdicts ``amend``, dispatch ``wf-feedback`` against
        the same task. Closes the routing-hint-but-no-action gap that
        existed in ADR-0038 for amend verdicts.

        Failures are logged but do not propagate.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_feedback_on_architect_amend,
        )
        try:
            await maybe_dispatch_feedback_on_architect_amend(
                session, self.dispatcher, step_id=step_id, typed=typed,
            )
        except Exception:
            logger.exception(
                "_maybe_fire_architect_amend_feedback: dispatch failed "
                "for step %s; prior projection committed, will retry on "
                "redelivery",
                step_id,
            )

    # ── Side-effect: emit review.override on architect accept-as-is ───────

    async def _maybe_emit_review_override(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """ADR-0038 architect override emitter.

        Delegates to ``triggers.maybe_emit_review_override_on_architect_completion``.
        Failures are logged but do not propagate — the prior projection
        has already committed and rolling back on an emission failure
        would lose the architect's verdict. The architect's own
        ``ON CONFLICT (id) DO NOTHING`` makes redelivery safe; the
        SQS retry on the original step.completed will rerun this
        helper on the next pass.
        """
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_emit_review_override_on_architect_completion,
        )
        try:
            await maybe_emit_review_override_on_architect_completion(
                session, step_id=step_id, typed=typed,
            )
        except Exception:
            logger.exception(
                "_maybe_emit_review_override: emission failed for step %s; "
                "prior projection committed, will retry on redelivery",
                step_id,
            )

    # ── Side-effect: emit validate.override on architect accept-as-is ─────

    async def _maybe_emit_validate_override(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """ADR-0042 architect validate-override emitter.

        Sibling to ``_maybe_emit_review_override``. Delegates to
        ``triggers.maybe_emit_validate_override_on_architect_completion``.
        Same failure semantics: logged, not propagated, idempotent
        on SQS redelivery via the trigger's ``ON CONFLICT DO NOTHING``.
        """
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_emit_validate_override_on_architect_completion,
        )
        try:
            await maybe_emit_validate_override_on_architect_completion(
                session, step_id=step_id, typed=typed,
            )
        except Exception:
            logger.exception(
                "_maybe_emit_validate_override: emission failed for step %s; "
                "prior projection committed, will retry on redelivery",
                step_id,
            )

    # ── ADR-0040: architect validator-tuning → wf-doc-amend ──────────────

    async def _maybe_dispatch_rule_tuning(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """ADR-0040: dispatch ``wf-doc-amend`` when an architect
        step.completed carries ``payload.validator_tuning``.

        Delegates to
        ``triggers.maybe_dispatch_rule_tuning_on_architect_completion``.
        Failures are logged but do not propagate — the prior projection
        has already committed; rolling that back on a dispatch failure
        would lose progress. Redelivery-safe via SQS retry on the
        original step.completed.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_rule_tuning_on_architect_completion,
        )
        try:
            await maybe_dispatch_rule_tuning_on_architect_completion(
                session, self.dispatcher, step_id=step_id, typed=typed,
            )
        except Exception:
            logger.exception(
                "_maybe_dispatch_rule_tuning: dispatch failed for step %s; "
                "prior projection committed, will retry on redelivery",
                step_id,
            )

    # ── ADR-0048: architect supersede → close PR + child task + wf-author ──

    async def _maybe_dispatch_supersede(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """ADR-0048: dispatch the supersede sequence when an architect
        step.completed carries ``payload.verdict='supersede'``.

        Delegates to
        ``triggers.maybe_dispatch_supersede_on_architect_verdict``. The
        trigger creates a child task carrying the architect's
        ``rewritten_description``, dispatches a fresh ``wf-author``
        against the child, and best-effort closes the parent's PR.

        Failures are logged but do not propagate — the prior projection
        has already committed; rolling that back on a dispatch failure
        would lose progress. Dedup gate keys on the parent task id so
        SQS redelivery cannot create N children against the same parent.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_supersede_on_architect_verdict,
        )
        try:
            await maybe_dispatch_supersede_on_architect_verdict(
                session,
                self.dispatcher,
                step_id=step_id,
                typed=typed,
                github_client=self.github_client,
            )
        except Exception:
            logger.exception(
                "_maybe_dispatch_supersede: dispatch failed for step %s; "
                "prior projection committed, will retry on redelivery",
                step_id,
            )

    # ── ADR-0058: gate-broken architect verdict → operator escalation ────

    async def _maybe_dispatch_gate_broken_escalation(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """ADR-0058: emit ``task.escalated_to_operator`` when an architect
        step.completed carries ``payload.verdict='gate-broken'``.

        Delegates to
        ``triggers.maybe_dispatch_gate_broken_escalation``. The trigger
        confirms the step belongs to wf-architecture-resolve, resolves
        the owning task, and persists an escalation event with
        ``reason='gate-broken'`` plus the gate's full stderr (from
        ``payload.gate_log_excerpt``) so the operator sees the actual
        tooling failure without re-running the loop.

        Failures are logged but do not propagate — the prior projection
        has already committed.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_gate_broken_escalation,
        )
        try:
            await maybe_dispatch_gate_broken_escalation(
                session,
                self.dispatcher,
                step_id=step_id,
                typed=typed,
            )
        except Exception:
            logger.exception(
                "_maybe_dispatch_gate_broken_escalation: dispatch failed for "
                "step %s; prior projection committed, will retry on redelivery",
                step_id,
            )

    # ── Self-trigger: wf-validate failure → wf-feedback / wf-doc-amend ────

    async def _maybe_fire_validate_feedback(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """Fire the appropriate convergence workflow when a
        ``wf-validate.step.completed`` arrives with ``decision='fail'``
        or ``'error'`` (ADR-0029 + fourth dispatch source).

        Routing for ``decision='fail'``:
          * If the ``docs-current-with-pr`` check is in the failing set
            → dispatch ``wf-doc-amend`` (fourth source).
          * Otherwise (different rule failures)
            → dispatch ``wf-feedback`` (existing path).

        ``decision='error'`` always dispatches ``wf-feedback`` unchanged.

        Skips cleanly when ``self.dispatcher`` is ``None`` (narrow tests
        that don't exercise dispatch). Failures are logged but do not
        propagate — the prior step's projection has already committed;
        rolling that back on a dispatch failure would lose progress. The
        dispatch helper's own retry / dedup logic handles transient errors
        on the next event delivery.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            is_docs_current_check_failure,
            maybe_dispatch_doc_amend_on_docs_check_fail,
            maybe_dispatch_feedback_on_terminal_failure,
        )
        try:
            if typed.output.decision == "fail":
                if is_docs_current_check_failure(typed.output):
                    # docs-current-with-pr check failed → route to wf-doc-amend
                    await maybe_dispatch_doc_amend_on_docs_check_fail(
                        session, self.dispatcher,
                        step_id=step_id, typed=typed,
                    )
                else:
                    # Different rule failure → wf-feedback (existing path)
                    await maybe_dispatch_feedback_on_terminal_failure(
                        session, self.dispatcher,
                        step_id=step_id, typed=typed,
                        workflow_id="wf-validate",
                        fail_decision="fail",
                    )
            elif typed.output.decision == "error":
                await maybe_dispatch_feedback_on_terminal_failure(
                    session, self.dispatcher,
                    step_id=step_id, typed=typed,
                    workflow_id="wf-validate",
                    fail_decision="error",
                )
        except Exception:
            logger.exception(
                "_maybe_fire_validate_feedback: dispatch failed for step %s; "
                "prior projection committed, will retry on redelivery",
                step_id,
            )

    # ── Self-trigger: wf-author failure → wf-feedback (ADR-0037) ─────────────

    async def _maybe_fire_author_feedback(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
    ) -> None:
        """Fire wf-feedback when a ``wf-author.step.completed`` arrives with
        ``decision='fail'`` (ADR-0037).

        Skips cleanly when ``self.dispatcher`` is ``None`` (narrow tests
        that don't exercise dispatch). Failures are logged but do not
        propagate — the prior step's projection has already committed;
        rolling that back on a dispatch failure would lose progress. The
        dispatch helper's own retry / dedup logic handles transient errors
        on the next event delivery.
        """
        if self.dispatcher is None:
            return
        if not isinstance(typed, StepCompleted):
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_feedback_on_terminal_failure,
        )
        try:
            await maybe_dispatch_feedback_on_terminal_failure(
                session, self.dispatcher,
                step_id=step_id, typed=typed,
                workflow_id="wf-author",
                fail_decision="fail",
            )
        except Exception:
            logger.exception(
                "_maybe_fire_author_feedback: dispatch failed for step %s; "
                "prior projection committed, will retry on redelivery",
                step_id,
            )

    async def _maybe_fire_author_feedback_on_step_failed(
        self,
        session: AsyncSession,
        step_id: str,
    ) -> None:
        """Fire wf-feedback when a ``wf-author.step.failed`` arrives.

        Sibling to ``_maybe_fire_author_feedback``. The existing helper
        keys on ``decision='fail'`` in the step's output payload — the
        worker COMPLETED but reported a failure verdict. This helper
        covers the silent-death case: the worker died/crashed before
        producing any output and the step ended at ``status='failed'``
        with no decision. Captured 2026-05-18 on task ``07c71852``:
        worker terminated mid-author, ADR-0037's existing trigger
        didn't fire because there was no decision='fail' payload, and
        operator nudge was the only recovery.

        Same dedup namespace and FEEDBACK_MAX_ATTEMPTS cap as the
        decision='fail' path.
        """
        if self.dispatcher is None:
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_feedback_on_step_failed,
        )
        try:
            await maybe_dispatch_feedback_on_step_failed(
                session, self.dispatcher,
                step_id=step_id,
                workflow_id="wf-author",
            )
        except Exception:
            logger.exception(
                "_maybe_fire_author_feedback_on_step_failed: dispatch "
                "failed for step %s; prior projection committed, will "
                "retry on redelivery",
                step_id,
            )

    async def _maybe_dispatch_terminal_step_failure(
        self,
        session: AsyncSession,
        step_id: str,
    ) -> None:
        """ADR-0062 Step 1: emit ``task.escalated_to_operator`` when a
        ``step.failed`` arrives on a run with no remaining steps to
        dispatch and no sibling escalation already covers the case.

        Delegates to
        ``triggers.maybe_dispatch_terminal_step_failure_escalation``.
        The trigger checks for remaining pending steps (returns silently
        if any remain — the cross-step loop will advance), and checks
        the dedup window against recent ``task.escalated_to_operator``
        events on the same task. Failures are logged but do not
        propagate — the prior projection has already committed.
        """
        if self.dispatcher is None:
            return
        from treadmill_api.coordination.triggers import (
            maybe_dispatch_terminal_step_failure_escalation,
        )
        try:
            await maybe_dispatch_terminal_step_failure_escalation(
                session, self.dispatcher, step_id=step_id,
            )
        except Exception:
            logger.exception(
                "_maybe_dispatch_terminal_step_failure: dispatch failed "
                "for step %s; prior projection committed, will retry on "
                "redelivery",
                step_id,
            )

    # ── Auto-merge cooling-off trigger (ADR-0031) ─────────────────────────────

    async def _maybe_fire_auto_merge(
        self,
        session: AsyncSession,
        step_id: str,
    ) -> None:
        """Set/push the auto-merge cooling-off deadline after step.completed.

        Delegates to ``maybe_auto_merge_on_mergeable`` which resolves the
        completing step's workflow, checks the mergeability VIEW, and writes
        (or refreshes) the Redis deadline key when all conditions pass.

        Skips cleanly when ``redis_client`` is not wired. Failures are logged
        but do not propagate — the prior step's projection has already
        committed; rolling that back on a deadline-write failure would lose
        progress. The next event delivery re-tries naturally.
        """
        if self.redis_client is None:
            return
        from treadmill_api.coordination.triggers import maybe_auto_merge_on_mergeable
        try:
            await maybe_auto_merge_on_mergeable(
                session,
                self.redis_client,
                step_id=step_id,
            )
        except Exception:
            logger.exception(
                "_maybe_fire_auto_merge: deadline update failed for step %s; "
                "prior projection committed, will retry on redelivery",
                step_id,
            )

    async def _maybe_fire_auto_merge_on_github(
        self,
        session: AsyncSession,
        *,
        repo: str,
        pr_number: int | None,
    ) -> None:
        """Set/push the auto-merge cooling-off deadline after a github event
        that can flip mergeability (ADR-0031 arming-coverage gap).

        Sibling to ``_maybe_fire_auto_merge`` (the ``step.completed`` arming
        seam). A task can cross into ``mergeable`` because CI finished green,
        a clean re-push landed, a conflict cleared, or a late ``pr_opened``
        arrived — none of which are ``step.completed`` events, so the
        step-based arming never re-fires and a green, approved PR strands.
        Delegates to ``maybe_auto_merge_on_github_event`` which resolves the
        task via ``task_prs`` and checks the mergeability VIEW.

        Skips cleanly when ``redis_client`` is not wired. Failures are logged
        but do not propagate — the github audit row has already committed; the
        next event delivery retries naturally.
        """
        if self.redis_client is None:
            return
        from treadmill_api.coordination.triggers import (
            maybe_auto_merge_on_github_event,
        )
        try:
            # Flush so the github event row this handler just persisted is
            # visible to the ``task_mergeability`` VIEW read inside the arming
            # body (same same-transaction-snapshot fix as the step path).
            await session.flush()
            await maybe_auto_merge_on_github_event(
                session,
                self.redis_client,
                repo=repo,
                pr_number=pr_number,
            )
        except Exception:
            logger.exception(
                "_maybe_fire_auto_merge_on_github: deadline update failed for "
                "%s#%s; prior projection committed, will retry on redelivery",
                repo, pr_number,
            )

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

    async def _cross_step_dispatch(
        self,
        session: AsyncSession,
        step_id: str,
    ) -> None:
        """When a step terminates (completed or failed), publish
        ``step.ready`` for the run's next pending step.

        The dispatcher (``dispatch.py``) handles single-step firing for
        the first step of a run; the consumer becomes the cross-step
        orchestrator from step 2 onward (ADR-0011's "consumer is the
        projector + cross-step orchestrator; dispatcher is single-shot",
        formalised by ADR-0015's cross-step dispatch section).

        Failure handling: any exception from the cross_step module is
        logged but does NOT propagate — the prior step's terminal
        status has already been applied to the row, and rolling that
        back on a downstream-dispatch failure would lose progress. The
        ``dispatch_publish_failed`` marker pattern inside
        ``dispatch_next_step`` records SNS/SQS failures so the replay
        loop heals; anything else (DB error, programming bug) is
        logged + swallowed here.

        Skips cleanly when ``self.dispatcher`` is ``None`` (narrow tests
        that don't exercise the cross-step path).
        """
        if self.dispatcher is None:
            return
        # Look up the terminating step row so we can pass run_id +
        # step_index to the cross_step module. The status UPDATE in
        # ``_dispatch_step`` ran earlier in this transaction so the row
        # is visible; re-delivery of an already-terminated step left it
        # untouched (the WHERE-clause idempotency) but the row's run_id
        # and step_index are still the correct lookup keys.
        try:
            step_uuid = uuid.UUID(str(step_id))
        except (ValueError, TypeError):
            logger.warning(
                "cross_step: malformed step_id %r; skipping", step_id,
            )
            return
        from treadmill_api.models import WorkflowRunStep as _WRS

        step = await session.get(_WRS, step_uuid)
        if step is None:
            logger.warning(
                "cross_step: step %s not found; skipping", step_id,
            )
            return

        from treadmill_api.coordination.cross_step import dispatch_next_step

        try:
            await dispatch_next_step(
                session,
                self.dispatcher,
                run_id=step.run_id,
                completed_step_index=step.step_index,
            )
        except Exception:
            logger.exception(
                "cross_step: dispatch_next_step raised for step %s "
                "(run %s, index %d); prior status already committed",
                step.id, step.run_id, step.step_index,
            )

    # ── Re-evaluation pass (D.6) ──────────────────────────────────────────────

    async def _reevaluate(self) -> None:
        """Run the post-event re-evaluation pass.

        Opens a fresh session so the dispatcher sees state committed by
        prior handlers in this poll cycle. The dispatcher's per-task
        SQL is read-then-write within that session, and we commit at
        the end.

        Swallowed-failure semantics: any exception inside the re-eval
        pass is logged but never propagates out of ``handle()``. The
        projection has already committed by this point; the next event
        delivery would re-run the projection (idempotent) but might
        not re-trigger the pass. We accept that trade-off because the
        replay loop (A.10) heals any missed dispatch.
        """
        if self.dispatcher is None:
            return
        from treadmill_api.coordination.redispatch import reevaluate

        try:
            async with self.sessionmaker() as session:
                dispatched = await reevaluate(session, self.dispatcher)
                await session.commit()
                if dispatched:
                    logger.info(
                        "coordination redispatch dispatched %d task(s): %s",
                        len(dispatched),
                        [str(t) for t in dispatched],
                    )
        except Exception:
            logger.exception("coordination redispatch pass failed; ignoring")

    # ── Probe helpers ─────────────────────────────────────────────────────────

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
