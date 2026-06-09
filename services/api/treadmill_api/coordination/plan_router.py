"""PlanRouter — routing decisions after a step projection commits.

Extracted from ``coordination/consumer.py`` per ADR-0084 §"Phase 3C" /
Task 2A Phase 2. The router owns every routing decision that consumes
the *committed* projection: every ``_maybe_*`` workflow-firing helper,
the cross-step dispatcher, the re-evaluation pass, the four
entity-type-specific event handlers (plan, github, schedule,
task-architect-emit-failure, task-terminal-for-triage), and the D.8
webhook-event drain.

Session ownership
-----------------
The router owns its own ``async_sessionmaker`` and opens a SEPARATE
session per ``route_*`` call. This is load-bearing for the auto-merge
race documented at the old ``consumer.py:559-569``: routing helpers
read the ``task_mergeability`` VIEW (and other event-projection
VIEWs), and the VIEW projects from the ``events`` table. The
``CoordinationConsumer`` commits the projection transaction BEFORE
delegating to the router, so the router's VIEW reads see the
just-committed state — same invariant the pre-extraction code relied
on via an explicit ``session.flush()`` then a continuation of the same
transaction.

Two transactions, one logical handler invocation. The projection
commits first (the audit row is the source of truth and must survive
even if routing crashes). The router opens its own session, runs its
decisions, and commits its own writes (dispatched-run audit rows,
internal Event INSERTs like ``review.override``, ``task_prs`` follow-
ons via ``_cross_step_dispatch``).

This file is a placeholder skeleton landing in the same PR as the
trace-replay equivalence harness. The 21 ``_maybe_*`` helpers,
``_cross_step_dispatch``, ``_reevaluate``, and the four ``_handle_*``
event-type handlers move from ``CoordinationConsumer`` to this class
across follow-up commits in this PR. The trace-replay harness gates
the merge: every extracted method's behavior is asserted equivalent
against a 1453-event captured trace before the PR lands.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import String, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from treadmill_api.coordination.event_projector import EventProjector
from treadmill_api.events.registry import (
    UnknownEventTypeError,
    parse_payload,
)
from treadmill_api.events.step import (
    StepCancelled,
    StepCompleted,
    StepFailed,
    StepSkipped,
    StepStarted,
)
from treadmill_api.models import Event, Task, TaskPR, WorkflowRun, WorkflowRunStep

logger = logging.getLogger("treadmill.coordination.router")


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


class PlanRouter:
    """Owns routing decisions that consume the committed event projection.

    Constructed once at lifespan-startup alongside ``CoordinationConsumer``.
    The consumer's ``handle()`` calls ``route_step()`` (or one of the
    entity-type-specific ``route_*`` methods) AFTER committing the
    projection transaction.

    Holds the same optional clients ``CoordinationConsumer`` does
    today — they're ``None`` in narrow tests, and each helper short-
    circuits cleanly when its required client is missing.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        projector: "EventProjector",
        redis_client: Any | None = None,
        publisher: Any | None = None,
        dispatcher: Any | None = None,
        github_client: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        # Routing helpers call ``persist_audit_row`` directly (rather than
        # going back through the consumer's ``_persist_event`` shim) to
        # avoid a circular dependency on CoordinationConsumer. The
        # projector is a pure writer with no client deps; safe to share.
        self.projector = projector
        self.redis_client = redis_client
        self.publisher = publisher
        self.dispatcher = dispatcher
        self.github_client = github_client
        self.settings = settings

    async def route_step(
        self,
        record: dict[str, Any],
        *,
        typed: Any,
        action: str | None,
        step_id: str,
    ) -> None:
        """Run every routing decision triggered by a step.* event.

        Opens its own session — separate transaction from the projection
        commit per the auto-merge race rationale (the VIEW reads consult
        the just-committed events table). Helpers fire in the exact
        ordering preserved from the pre-extraction handle().
        """
        async with self.sessionmaker() as session:
            if action == "completed":
                await self._write_task_prs_on_completed_routing(
                    session, step_id, typed, record.get("payload") or {},
                )
                await self._maybe_fire_review_feedback(session, step_id, typed)
            if action == "completed":
                await self._maybe_fire_validate_feedback(session, step_id, typed)
            if action == "completed":
                await self._maybe_fire_author_feedback(session, step_id, typed)
            if action == "failed":
                await self._maybe_fire_author_feedback_on_step_failed(
                    session, step_id,
                )
            if action == "failed":
                await self._maybe_dispatch_terminal_step_failure(
                    session, step_id,
                )
            if action == "completed":
                await self._maybe_fire_deadlock_arbitration(
                    session, step_id, typed,
                )
            if action == "completed":
                await self._maybe_fire_feedback_validation_fail_arbitration(
                    session, step_id, typed,
                )
            if action == "completed":
                await self._maybe_fire_feedback_no_progress_arbitration(
                    session, step_id, typed,
                )
            if action == "completed":
                await self._maybe_escalate_terminal_give_up(
                    session, step_id, typed,
                )
            if action == "completed":
                await self._maybe_emit_review_override(
                    session, step_id, typed,
                )
            if action == "completed":
                await self._maybe_emit_validate_override(
                    session, step_id, typed,
                )
            if action == "completed":
                await self._maybe_dispatch_rule_tuning(
                    session, step_id, typed,
                )
            if action == "completed":
                await self._maybe_dispatch_supersede(
                    session, step_id, typed,
                )
            if action == "completed":
                await self._maybe_dispatch_gate_broken_escalation(
                    session, step_id, typed,
                )
            if action == "completed":
                await self._maybe_fire_architect_amend_feedback(
                    session, step_id, typed,
                )
            if action == "completed":
                # Auto-merge race (orig consumer.py:559-569): pre-extraction
                # required an explicit ``await session.flush()`` here so the
                # ``task_mergeability`` VIEW saw the just-INSERTed override /
                # verdict rows. The two-transaction design preserves the
                # invariant without the flush: the projection committed
                # before this router session opened, so VIEW reads see the
                # committed state directly.
                await self._maybe_fire_auto_merge(session, step_id)
            if action in ("completed", "failed"):
                await self._cross_step_dispatch(session, step_id)
            await session.commit()

        if action == "completed":
            await self._reevaluate()

    async def _write_task_prs_on_completed_routing(
        self,
        session: AsyncSession,
        step_id: str,
        typed: Any,
        payload: dict[str, Any],
    ) -> None:
        """Mirror of CoordinationConsumer._write_task_prs_on_completed
        for the router path: write the task_prs row + drain pending
        webhook events. The projector owns the INSERT; the drain is a
        routing concern (publishes new SQS messages that re-enter
        handle())."""
        from treadmill_api.webhooks.pending_events import (
            drain_pending_events,
            pr_pending_buffer_key,
        )
        written = await self.projector.write_task_prs(
            session, step_id, typed, payload,
        )
        if written is None:
            return
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

    async def route_plan(
        self,
        record: dict[str, Any],
        *,
        action: str | None,
        payload: dict[str, Any],
    ) -> None:
        if action == "activated":
            await self._handle_plan_activated(record, payload=payload)
        else:
            logger.debug("coordination ignoring plan.%s", action)

    async def route_github(
        self,
        record: dict[str, Any],
        *,
        action: str | None,
        payload: dict[str, Any],
    ) -> None:
        await self._handle_github_event(
            record, action=action, payload=payload,
        )

    async def route_schedule(
        self,
        record: dict[str, Any],
        *,
        action: str | None,
        payload: dict[str, Any],
    ) -> None:
        await self._handle_schedule_event(
            record, action=action, payload=payload,
        )

    async def route_task_architect_emit_failure(
        self,
        record: dict[str, Any],
        *,
        payload: dict[str, Any],
    ) -> None:
        await self._handle_architect_emit_failure(record, payload=payload)

    async def route_task_terminal_for_triage(
        self,
        record: dict[str, Any],
        *,
        action: str | None,
        payload: dict[str, Any],
    ) -> None:
        await self._handle_task_terminal_for_triage(
            record, action=action, payload=payload,
        )

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
            await self.projector.persist_audit_row(session, record, payload)

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
            await self.projector.persist_audit_row(session, record, payload)

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
