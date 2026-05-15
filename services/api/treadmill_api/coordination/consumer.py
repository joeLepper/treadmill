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
from treadmill_api.events.step import (
    StepCancelled,
    StepCompleted,
    StepFailed,
    StepStarted,
)
from treadmill_api.models import Event, Task, TaskPR, WorkflowRun, WorkflowRunStep
from treadmill_api.webhooks.pending_events import drain_pending_events

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
            # ADR-0031: when a wf-validate or wf-review step completes,
            # set/push the auto-merge cooling-off deadline in Redis. The
            # 5s poll loop (``_auto_merge_loop``) fires the merge when
            # the 30s window elapses without a reset.
            if action == "completed":
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

            # ── Conflict sweep (Week 3 B.3, pr_merged only) ──────────────
            if action == "pr_merged":
                await self._sweep_after_pr_merged(session, typed)
                # Task #124 — branch-name fallback for operator-completed PRs.
                # Runs after the sweep so conflicts are resolved first.
                await self._try_task_prs_fallback_on_pr_merged(session, typed)

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
                    stored_repo,
                    typed.pr_number,
                    task_id,
                )
            except Exception:
                logger.exception(
                    "task_prs fallback: drain_pending_events failed for "
                    "repo=%s pr_number=%d task_id=%s; row still committed",
                    stored_repo, typed.pr_number, task_id,
                )

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
            await session.execute(
                update(WorkflowRunStep)
                .where(
                    WorkflowRunStep.id == step_id,
                    WorkflowRunStep.status.in_(("pending", "running")),
                )
                .values(
                    status="completed",
                    completed_at=typed.completed_at,
                    output=output_to_store,
                )
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
                    repo,
                    pr_number,
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
