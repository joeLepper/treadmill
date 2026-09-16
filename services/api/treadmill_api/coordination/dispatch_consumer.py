"""Event→dispatch consumer — ADR-0118 phase 1, the wake-gap fix.

Phase-0 found the root of the recurring wake-gap: the server DELIVERS every task-scoped
completion event (``github.pr_merged``, ``task.ci_result``, evaluator verdicts) but nothing
server-side ACTS on a delivered event — only the coordinator AGENT's turn does, so a wake is
lost whenever that turn is idle. This background subscriber closes it by construction: it
consumes each delivered event and does the next dispatch deterministically, with NO agent
turn to miss.

Design (ADR-0118):
* One dispatch WRITER (this consumer). The judgment stays in agents — ``is_depends_on_satisfied``
  and ``is_ci_ready`` (``dispatch_predicates``) are pure decision functions this calls; the
  workers + validator keep peer review / verdicts.
* Idempotent by construction: a dispatch is an INSERT of a ``task_executions`` row at the
  task's current ``generation``; the partial UNIQUE index ``(task_id, generation)`` over
  author triggers makes a re-delivered event a database no-op (migration 20260915_0100). A
  genuine rework bumps ``tasks.generation`` first, so the next dispatch is a new row.
* Per-plan substrate (SC6): acts ONLY on plans whose ``substrate == 'router'``; the legacy
  agent-coordinator owns the rest. The binding is read per event but is immutable once set at
  standup, so there is no cross-substrate double-dispatch.
* DURABLE by RECONCILIATION, not by the queue. ``subscribe_local`` is an in-process asyncio
  queue — a fast path, not a durable one: a wake published while this consumer is down, or
  lost when a crash lands between the per-dependent commits, is gone from that queue with no
  redelivery, which would REINTRODUCE the wake-gap (Bert's review, 2026-09-15). So the live
  event path is backed by a periodic RECONCILE SWEEP (``reconcile``) over the DURABLE facts:
  it dispatches any router-substrate task whose ``depends_on`` is satisfied but which has NO
  author ``task_executions`` row at its current generation. That closes the down-consumer and
  crash-mid-loop holes — and, because it reads the DB rather than one process's queue, the
  cross-process/multi-replica miss too (a webhook persisted on another replica). The sweep is
  idempotent with the live path via the same ``(task_id, generation)`` unique index, so the
  two never double-dispatch. SC2 ("wake-gap cannot recur") holds on the sweep, not on the
  consumer being perfectly live.

DEPLOYMENT: the live event path assumes ONE designated consumer process (in-process bus);
the reconcile sweep is what makes correctness independent of that assumption.

Lifecycle mirrors ``FabricEventSink``/``NotificationFanout``: ``start()`` is a no-op on a dark
build (router dispatch disabled), ``stop()`` is safe on a never-started instance.

INCREMENTS (this file): the ``github.pr_merged`` → dependent-dispatch path + the reconcile
sweep; ``task.ci_result`` → re-eval dispatch (via ``is_ci_ready``, Bert); and the evaluator
verdict loop — ``rework`` bumps ``tasks.generation`` + re-dispatches the author,
``approve`` records the approval (idempotent BY CONSTRUCTION via ``verdict_applications``
UNIQUE(task_id, head_sha)) and, for a ``feature-branch`` repo with a git ``runner_factory`` wired,
INTEGRATES the approved head (``git merge --no-ff`` + push via ``integration_merger``) — DARK
until the enable step wires the runner (creds/identity). Follow-on: ``main``-mode integration
(``gh pr merge``) and the prod runner-factory wiring.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import IntegrityError

from treadmill_api.coordination.dispatch_predicates import (
    is_depends_on_satisfied,
    on_ci_result,
)
from treadmill_api.coordination.integration_merger import (
    GitRunner,
    integrate_task,
    merge_op_for_plan,
)
from treadmill_api.dispatch import Dispatcher
from treadmill_api.eventbus import subscribe_local, unsubscribe_local
from treadmill_api.events.registry import encode_payload
from treadmill_api.events.task import TaskEscalatedToOperator, TaskReady
from treadmill_api.models.evaluator_dispatch import EvaluatorDispatch
from treadmill_api.models.event import Event
from treadmill_api.models.plan import Plan
from treadmill_api.models.task import Task, TaskPR
from treadmill_api.models.task_execution import TaskExecution
from treadmill_api.models.team_config import TeamConfig
from treadmill_api.models.verdict_application import VerdictApplication

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verdict:
    """An evaluator verdict, normalized from a ``task.evaluator_verdict`` delivery record.

    Pure value: parsed by ``parse_verdict`` (DB-free, foil-tested); the consumer's
    ``_on_evaluator_verdict`` fetches the head-SHA fallback and applies the side effects.
    ``head_sha`` may be ``None`` here when the poster omitted it — the consumer back-fills it
    from the task's newest open PR before it keys the idempotency marker.
    """

    task_id: str
    decision: str  # "approve" | "rework"
    head_sha: str | None
    remediation: str | None


def _coerce_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Return the record's payload as a dict. The local bus may deliver it as a JSON string;
    tolerate that (mirrors ``dispatch_predicates._head_sha_of``)."""
    payload = record.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = None
    return payload if isinstance(payload, dict) else {}


def parse_verdict(record: dict[str, Any]) -> Verdict | None:
    """Parse a ``task.evaluator_verdict`` delivery into a ``Verdict``, or ``None`` if the record
    is not a well-formed verdict (wrong type, missing task_id, or an unknown decision value).

    Pure and total: never raises on a malformed record — a bad verdict is dropped, not fatal.
    """
    if record.get("entity_type") != "task" or record.get("action") != "evaluator_verdict":
        return None
    task_id = record.get("task_id")
    if not task_id:
        return None
    payload = _coerce_payload(record)
    decision = payload.get("verdict") or payload.get("decision")
    if decision not in ("approve", "rework"):
        return None
    head = payload.get("head_sha") or payload.get("commit_sha") or record.get("commit_sha")
    return Verdict(
        task_id=str(task_id),
        decision=str(decision),
        head_sha=str(head) if head else None,
        remediation=payload.get("remediation"),
    )


def select_worker(
    roster: list[str], load_by_worker: dict[str, int], prior: str | None
) -> str | None:
    """Pick the worker to dispatch a task to (pure core; ADR-0118 worker-assignment, a port of
    the legacy coordinator template §4 "Worker routing").

    CONTINUITY first: if ``prior`` (the worker who served an earlier generation of THIS task) is
    still in the roster, reuse it — a rework must return to the worker that has the context, and
    reassigning it would throw that away. Otherwise LOAD-BALANCE: the roster worker with the
    fewest in-flight tasks, tie-broken by roster order so the choice is deterministic (the
    "round-robin among ties" rule). Returns ``None`` when the roster is empty (no team config) —
    the caller then falls back to a synthetic label. Pure + DB-free so it is foil-tested directly.
    """
    if prior is not None and prior in roster:
        return prior
    if not roster:
        return None
    return min(roster, key=lambda w: (load_by_worker.get(w, 0), roster.index(w)))


def integration_slug(doc_path: str | None) -> str | None:
    """Derive the per-plan integration-branch slug from the plan's ``doc_path`` (pure core;
    ADR-0118 approve→integration, a port of the coordinator template §3.1a).

    §3.1a: the branch is ``joes-agents/<branch-slug>`` where ``<branch-slug>`` is the plan doc's
    basename WITH its date prefix and WITHOUT the ``.md`` extension — e.g.
    ``docs/plans/2026-09-13-fix-auth.md`` → ``2026-09-13-fix-auth``. Returns ``None`` when there
    is no ``doc_path`` (a plan that never recorded one cannot have a derived branch — the caller
    leaves the approval recorded for later rather than integrating to a guessed branch).

    NOTE: §3.1a's collision rule (append ``-<plan_id[:8]>`` when two docs share a basename) is a
    follow-on — it needs a cross-plan uniqueness check this pure core does not have.
    """
    if not doc_path:
        return None
    base = doc_path.rsplit("/", 1)[-1]
    if base.endswith(".md"):
        base = base[:-3]
    return base or None


# git-check-ref-format forbids these in a ref component: space and the metacharacters
# ~ ^ : ? * [ \, plus control chars; and the sequences .. and @{, a trailing .lock, a leading/
# trailing dot, and empty. We reject rather than sanitize (a rewrite could collide two docs).
_REF_FORBIDDEN = set(" ~^:?*[\\") | {chr(c) for c in range(0x20)} | {chr(0x7F)}


def is_valid_ref_component(slug: str) -> bool:
    """True iff ``slug`` is a legal single git ref component (so ``joes-agents/<slug>`` is a
    pushable branch). A doc_path basename with an invalid char would make ``integrate_task``
    infra-fail forever, so the caller escalates instead of looping on it (Bert #418, pure core).
    """
    if not slug or ".." in slug or "@{" in slug:
        return False
    if slug.startswith(".") or slug.endswith(".") or slug.endswith(".lock"):
        return False
    return not any(ch in _REF_FORBIDDEN for ch in slug)

# The task-scoped completion events that can unblock or re-trigger work. Increment 1 acts on
# pr_merged; the others are recognized and routed to their (stubbed) handlers so the classify
# path is complete and testable now.
_HANDLED = {
    ("github", "pr_merged"),
    ("task", "ci_result"),
    ("task", "evaluator_verdict"),
    ("run", "completed"),
    ("task", "completed"),
}


class DispatchConsumer:
    """Background eventbus subscriber that turns a delivered completion event into the next
    deterministic dispatch. See the module docstring for the ADR-0118 design."""

    def __init__(
        self,
        *,
        session_factory: Any = None,
        dispatcher: Dispatcher | None = None,
        enabled: bool = False,
        reconcile_interval_seconds: float = 30.0,
        runner_factory: Callable[[str], Awaitable[GitRunner]] | None = None,
    ) -> None:
        # Dark by default: an unset router-dispatch flag means no subscription and no task,
        # exactly like FabricEventSink's no-URL build.
        self._session_factory = session_factory
        # The durable launch path: persist_and_publish a task.ready event (source-of-truth
        # Event row + bus publish, replay-resilient) so the assigned worker session — which
        # subscribes by its label — picks the task up. Without a dispatcher the consumer still
        # records the dispatch row (SC2/SC3) but does not emit the launch signal.
        self._dispatcher = dispatcher
        # The git executor for approve→integration (feature-branch merge). A factory
        # ``repo -> GitRunner`` so the working clone is built lazily per repo. None (prod today,
        # and any test that doesn't exercise integration) => the approve path records the approval
        # but does NOT merge — the same "wired but off" shape as the dispatcher launch signal.
        self._runner_factory = runner_factory
        self._enabled = enabled and session_factory is not None
        self._reconcile_interval = reconcile_interval_seconds
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def is_configured(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if not self._enabled:
            logger.info(
                "dispatch consumer: router dispatch disabled; consumer is dark, skipping start"
            )
            return
        self._stopped = False
        # Subscribe BEFORE spawning the task so the queue exists when the task awaits get().
        self._queue = subscribe_local()
        self._task = asyncio.create_task(self._run(), name="dispatch-consumer")
        # The reconcile sweep is the durable backstop for the in-process queue (see the
        # module docstring): it makes SC2 hold even across a down consumer or a crash mid-loop.
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="dispatch-reconcile"
        )
        logger.info("dispatch consumer started: router owns dispatch for router-substrate plans")

    async def stop(self) -> None:
        self._stopped = True
        if self._queue is not None:
            unsubscribe_local(self._queue)
        for attr in ("_task", "_reconcile_task"):
            t: asyncio.Task[None] | None = getattr(self, attr)
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("dispatch consumer raised on shutdown")
                setattr(self, attr, None)
        self._queue = None
        logger.info("dispatch consumer stopped")

    async def _reconcile_loop(self) -> None:
        """Periodically run the reconcile sweep — the durable backstop for the in-process
        queue. Any failure is contained; the loop must outlive a transient DB hiccup."""
        while not self._stopped:
            try:
                await asyncio.sleep(self._reconcile_interval)
            except asyncio.CancelledError:
                raise
            if self._stopped or self._session_factory is None:
                continue
            try:
                async with self._session_factory() as session:
                    await self.reconcile(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dispatch consumer: reconcile sweep raised; continuing")
            # The integration sweep is the retry backstop for approve→integration, the analog of
            # reconcile for dispatch (Bert #418): after an approval claim commits, a crash or
            # infra-fail during integration strands the task — a re-delivered verdict no-ops on
            # the committed claim, so nothing else re-drives it. The sweep re-drives every
            # approved-but-not-integrated task; integrate_task is idempotent (ancestry no-op), so
            # re-driving an already-merged one is a safe "already-integrated". Skipped when dark.
            if self._runner_factory is None:
                continue
            try:
                async with self._session_factory() as session:
                    await self.integration_sweep(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dispatch consumer: integration sweep raised; continuing")

    async def _run(self) -> None:
        assert self._queue is not None
        while not self._stopped:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self.handle(record)
            except Exception:
                # A bug in the dispatch logic must never take the loop down (mirrors
                # FabricEventSink). Per-event failures are contained here.
                logger.exception("dispatch consumer: handle raised; continuing")

    async def handle(self, record: dict[str, Any]) -> None:
        """Public entry point (exposed for tests, no asyncio loop needed).

        Classifies the event and routes to the matching dispatch handler. Non-completion
        events and events for non-router plans are dropped.
        """
        key = (record.get("entity_type"), record.get("action"))
        if key not in _HANDLED:
            return
        if self._session_factory is None:
            return

        async with self._session_factory() as session:
            if key == ("github", "pr_merged"):
                await self._on_upstream_terminal(session, record)
            elif key == ("task", "ci_result"):
                # TRACE 2 — the ci_result wake. Gate substrate here (Bert's split: the consumer
                # gates, on_ci_result decides), then let the handler fire the evaluator when the
                # required checks are ready. The per-(task, head) dedup lives in the write.
                task_id = record.get("task_id")
                if task_id and await self._is_router_task(session, str(task_id)):
                    await on_ci_result(session, record, self._dispatch_evaluator)
            elif key == ("task", "evaluator_verdict"):
                # The verdict loop: rework → bump generation + re-dispatch the author; approve →
                # record the approval for integration. Substrate is gated inside (SC6).
                await self._on_evaluator_verdict(session, record)
            elif key in {("run", "completed"), ("task", "completed")}:
                await self._on_upstream_terminal(session, record)

    async def reconcile(self, session: Any) -> int:
        """The durable backstop: dispatch every router-substrate task whose deps are satisfied
        but which has NO author ``task_executions`` row at its current generation. Idempotent
        with the live path via the ``(task_id, generation)`` unique index. Returns the count
        dispatched (0 in steady state). This — not the in-process queue — is why SC2 holds
        across a down consumer, a crash mid-loop, or a cross-replica publish.

        Exposed (like ``handle``) so tests can drive one sweep without the timer loop.
        """
        candidates = (
            await session.execute(
                text(
                    "SELECT t.id FROM tasks t "
                    "JOIN plans p ON p.id = t.plan_id "
                    "WHERE p.substrate = 'router' "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM task_executions te "
                    "    WHERE te.task_id = t.id AND te.generation = t.generation "
                    "      AND te.trigger IN ('initial','coordinator-rework','evaluator-rework')"
                    "  )"
                    # A task whose PR has merged is DONE — never re-dispatch it, even if it
                    # somehow lacks a current-generation execution row (e.g. an upstream that
                    # completed). The live path dispatches dependents, not the merged task; the
                    # sweep scans all tasks, so it needs this terminal guard.
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM events e "
                    "    WHERE e.task_id = t.id AND e.action = 'pr_merged'"
                    "  )"
                )
            )
        ).all()
        dispatched = 0
        for (task_id,) in candidates:
            if await self._maybe_dispatch(session, str(task_id)):
                dispatched += 1
        if dispatched:
            logger.info("dispatch consumer: reconcile sweep dispatched %d task(s)", dispatched)
        return dispatched

    async def _on_upstream_terminal(self, session: Any, record: dict[str, Any]) -> None:
        """An upstream task reached a terminal fact (pr_merged / run.completed / completed):
        dispatch every dependent whose ``depends_on`` is now fully satisfied."""
        upstream_id = record.get("task_id")
        if not upstream_id:
            return
        upstream_id = str(upstream_id)

        # Candidate dependents: tasks whose dependency expression references this upstream id.
        # (Expressions are stored with the upstream's resolved UUID, per routers/plans.py.)
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT task_id FROM task_dependencies "
                    "WHERE expression LIKE '%' || :uid || '%'"
                ),
                {"uid": upstream_id},
            )
        ).all()
        for (dep_task_id,) in rows:
            await self._maybe_dispatch(session, str(dep_task_id))

    async def _maybe_dispatch(self, session: Any, task_id: str) -> bool:
        """Dispatch ``task_id`` iff it is on the router substrate and every dependency is
        satisfied. Idempotent: a second delivery for the same (task, generation) no-ops on the
        unique index. Returns True iff a new dispatch row was created."""
        task = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            return False
        if not await self._is_router_plan(session, task.plan_id):
            return False  # SC6 — the legacy coordinator owns non-router plans.
        if not await is_depends_on_satisfied(session, task_id):
            return False
        return await self._dispatch(session, task, trigger="initial")

    async def _is_router_plan(self, session: Any, plan_id: Any) -> bool:
        substrate = (
            await session.execute(select(Plan.substrate).where(Plan.id == plan_id))
        ).scalar_one_or_none()
        return substrate == "router"

    async def _is_router_task(self, session: Any, task_id: str) -> bool:
        """SC6 gate for a task-scoped event that carries only ``task_id`` (e.g. ci_result):
        the task's plan must be on the router substrate."""
        substrate = (
            await session.execute(
                select(Plan.substrate).join(Task, Task.plan_id == Plan.id).where(
                    Task.id == task_id
                )
            )
        ).scalar_one_or_none()
        return substrate == "router"

    async def _dispatch_evaluator(self, *, task_id: str, head_sha: str) -> bool:
        """The WRITE half of the ci_result→re-eval path (injected into ``on_ci_result``).

        Records ONE evaluator dispatch per (task, head): the UNIQUE(task_id, head_sha) makes a
        trailing non-required suite's re-entry a no-op, so the evaluator fires exactly once per
        head. A rework push is a new head_sha → a new evaluation. Runs in its own session (the
        handler passes no session). Returns True iff a new dispatch was recorded.
        """
        if self._session_factory is None:
            return False
        async with self._session_factory() as session:
            session.add(EvaluatorDispatch(task_id=task_id, head_sha=head_sha))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.debug(
                    "dispatch consumer: evaluator already dispatched task=%s head=%s (no-op)",
                    task_id,
                    head_sha,
                )
                return False
            logger.info(
                "dispatch consumer: evaluator dispatched task=%s head=%s", task_id, head_sha
            )
            return True

    async def _on_evaluator_verdict(self, session: Any, record: dict[str, Any]) -> None:
        """Apply an evaluator verdict for a router task (the ADR-0118 verdict loop).

        ``rework`` bumps ``tasks.generation`` and re-dispatches the author at the new
        generation; ``approve`` records the approval. EXACTLY-ONCE BY CONSTRUCTION: the apply
        INSERTs a ``verdict_applications`` row (UNIQUE(task_id, head_sha)) FIRST, so a
        re-delivered, double-POSTed, OR concurrently-delivered (multi-replica) verdict fails at
        the INSERT and no-ops — no SELECT-then-act race, matching the ``(task_id, generation)``
        author index and ``evaluator_dispatches`` (Bert review, PR #416). The whole apply (claim
        + bump + author dispatch + task.ready) commits in ONE transaction via ``_dispatch``, so
        the bump and the new author row can never land apart. SC6: router-substrate only.
        """
        verdict = parse_verdict(record)
        if verdict is None:
            return
        if not await self._is_router_task(session, verdict.task_id):
            return  # SC6 — the legacy coordinator owns non-router plans.
        task = (
            await session.execute(select(Task).where(Task.id == verdict.task_id))
        ).scalar_one_or_none()
        if task is None:
            return
        head_sha = verdict.head_sha or await self._resolve_head_sha(session, verdict.task_id)
        if head_sha is None:
            # A verdict with no head — and no open PR to borrow one from — cannot be keyed or
            # targeted, so we cannot apply it. Dropping it silently would STALL the task (a
            # rework never re-dispatches, an approve never records) with no signal. Make it
            # LOUD: a durable operator escalation, not just a log line (Bert review).
            await self._escalate_undeliverable_verdict(session, task)
            return
        if not await self._claim_verdict(session, task, head_sha, verdict.decision):
            return  # already applied (this or a concurrent delivery won) — idempotent no-op.
        if verdict.decision == "rework":
            await self._apply_rework(session, task)
        else:
            await self._record_approval(session, task, head_sha)

    async def _resolve_head_sha(self, session: Any, task_id: str) -> str | None:
        """Fall back to the task's newest open PR head when the verdict omitted ``head_sha``.
        Newest by ``created_at`` and only OPEN PRs (``closed_at IS NULL``) so a stale merged PR
        never supplies the head for a fresh verdict."""
        row = (
            await session.execute(
                select(TaskPR.head_sha)
                .where(TaskPR.task_id == task_id, TaskPR.closed_at.is_(None))
                .order_by(TaskPR.created_at.desc())
                .limit(1)
            )
        ).first()
        return str(row[0]) if row and row[0] else None

    async def _claim_verdict(
        self, session: Any, task: Task, head_sha: str, decision: str
    ) -> bool:
        """Claim the (task, head) verdict by INSERTing the ``verdict_applications`` row. Returns
        True iff THIS call created the row; False if it already existed (the unique constraint
        no-ops a re-delivery or a concurrent apply). The INSERT runs in a SAVEPOINT so a
        violation rolls back to it WITHOUT poisoning the outer transaction — the same pattern as
        ``_dispatch``'s author insert. ``applied_generation`` is the CURRENT (pre-bump)
        generation: on rework the caller bumps to +1 next, so this records the retired one."""
        try:
            async with session.begin_nested():
                session.add(
                    VerdictApplication(
                        task_id=task.id,
                        head_sha=head_sha,
                        decision=decision,
                        applied_generation=task.generation,
                    )
                )
                await session.flush()
        except IntegrityError:
            logger.debug(
                "dispatch consumer: verdict already applied task=%s head=%s (no-op)",
                task.id, head_sha,
            )
            return False
        return True

    async def _apply_rework(self, session: Any, task: Task) -> None:
        """rework verdict (already claimed): bump the generation and re-dispatch the author at
        the new generation with the ``evaluator-rework`` trigger. The re-dispatched worker reads
        the verdict's remediation on its wake (the router does not push a brief). ``_dispatch``
        commits the whole unit (claim + bump + new author row + task.ready) atomically."""
        task.generation = task.generation + 1
        await session.flush()
        await self._dispatch(session, task, trigger="evaluator-rework")

    async def _record_approval(self, session: Any, task: Task, head_sha: str) -> None:
        """approve verdict (already claimed): commit the claim, then attempt integration.

        The claim commits FIRST so an approval is durable even if integration fails or crashes —
        the ``verdict_applications`` row is the work-list an integrator (this call, a retry, or a
        future sweep) consumes. Integration itself (``_maybe_integrate``) is separate and
        non-transactional (git is external): a feature-branch ``git merge --no-ff`` + push of the
        approved head, which — being contained in the integration branch — closes the PR and
        fires ``github.pr_merged``, unblocking dependents through the normal terminal path."""
        await session.commit()
        logger.info("dispatch consumer: recorded approval task=%s head=%s", task.id, head_sha)
        await self._maybe_integrate(session, task, head_sha)

    async def _maybe_integrate(self, session: Any, task: Task, head_sha: str) -> None:
        """Feature-branch approve→integration (ADR-0118). DARK unless a git ``runner_factory`` is
        wired: with none, the approval is recorded and integration is deferred (the work-list row
        remains). Only ``merge_target == 'feature-branch'`` integrates here; ``main`` mode
        (``gh pr merge``) is a follow-on. On a real conflict the router escalates (a worker must
        resolve it); an infra failure is left for retry (the approval row persists)."""
        if self._runner_factory is None:
            logger.info(
                "dispatch consumer: integration not wired (dark); approval recorded for task=%s",
                task.id,
            )
            return
        merge_target = await self._merge_target(session, task.repo)
        if merge_target != "feature-branch":
            logger.info(
                "dispatch consumer: repo=%s merge_target=%s — main-mode integration is a "
                "follow-on; approval recorded for task=%s", task.repo, merge_target, task.id,
            )
            return
        slug = integration_slug(await self._plan_doc_path(session, task.plan_id))
        if slug is None:
            logger.warning(
                "dispatch consumer: task=%s plan has no doc_path → no integration branch slug; "
                "approval recorded, integration deferred", task.id,
            )
            return
        if not is_valid_ref_component(slug):
            # A doc_path basename with a git-invalid char yields an unpushable branch name, so
            # integrate_task would infra-fail FOREVER (Bert #418). Escalate instead of looping.
            logger.warning(
                "dispatch consumer: task=%s slug %r is not a legal git ref component; escalating",
                task.id, slug,
            )
            await self._escalate(session, task, "integration_blocked")
            return
        merge_op = await merge_op_for_plan(session, task.plan_id, slug, head_sha)
        runner = await self._runner_factory(task.repo)
        result = await integrate_task(runner, merge_op)
        if result in ("merged", "already-integrated"):
            logger.info(
                "dispatch consumer: integrated task=%s head=%s into %s (%s); pr_merged will "
                "unblock dependents", task.id, head_sha, merge_op.integration_branch, result,
            )
        elif result == "conflict":
            # A real textual conflict needs a worker's judgment — the router never resolves one.
            await self._escalate(session, task, "integration_conflict")
        else:
            # fetch-failed / merge-failed / push-rejected: infra/transient. Leave the approval
            # row as the work-list; a retry (or a future integration sweep) re-drives it.
            logger.warning(
                "dispatch consumer: integration of task=%s into %s returned %s (infra); "
                "approval remains for retry", task.id, merge_op.integration_branch, result,
            )

    async def integration_sweep(self, session: Any) -> int:
        """Re-drive every approved-but-not-integrated task — the retry backstop for
        approve→integration (Bert #418), the analog of ``reconcile`` for dispatch.

        A ``verdict_applications`` row with ``decision='approve'`` on a router plan whose task has
        NO ``github.pr_merged`` event is an approval whose integration has not (yet) landed —
        either never attempted (a crash after the claim commit) or an infra-fail. The sweep
        re-runs ``_maybe_integrate`` for each; ``integrate_task`` is idempotent (the ancestry
        check no-ops an already-merged head), so re-driving is safe. Returns the candidate count.
        Exposed (like ``reconcile``) so a test drives one pass without the timer.
        """
        rows = (
            await session.execute(
                text(
                    # DISTINCT ON (task) + created_at DESC → the LATEST approved head per task
                    # (Bert #419): a re-approval (stranded at H1, then approved again at H2) must
                    # integrate ONLY H2 — never re-merge the superseded H1.
                    "SELECT DISTINCT ON (va.task_id) va.task_id, va.head_sha "
                    "FROM verdict_applications va "
                    "JOIN tasks t ON t.id = va.task_id "
                    "JOIN plans p ON p.id = t.plan_id "
                    "WHERE va.decision = 'approve' AND p.substrate = 'router' "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM events e "
                    "    WHERE e.task_id = t.id AND e.action = 'pr_merged'"
                    "  )"
                    # Exclude a STUCK approval (Bert #419): once integration escalated a real
                    # conflict or a blocked (unpushable) slug, re-driving it every tick would
                    # re-escalate forever (dashboard spam) and never succeed — a human must clear
                    # it (which re-dispatches via a fresh verdict). Stop re-driving, don't back off.
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM events e2 "
                    "    WHERE e2.task_id = t.id AND e2.action = 'escalated_to_operator' "
                    "      AND e2.payload->>'reason' IN "
                    "          ('integration_conflict','integration_blocked')"
                    "  ) "
                    "ORDER BY va.task_id, va.created_at DESC"
                )
            )
        ).all()
        for task_id, head_sha in rows:
            task = (
                await session.execute(select(Task).where(Task.id == task_id))
            ).scalar_one_or_none()
            if task is not None:
                await self._maybe_integrate(session, task, str(head_sha))
        if rows:
            logger.info("dispatch consumer: integration sweep re-drove %d approval(s)", len(rows))
        return len(rows)

    async def _merge_target(self, session: Any, repo: str) -> str:
        """The repo's integration mode (``team_configs.merge_target``): ``feature-branch``
        (default) or ``main``. Defaults to ``feature-branch`` when no team config (the safe
        common case — the router only integrates feature-branch here)."""
        row = (
            await session.execute(
                select(TeamConfig.merge_target).where(TeamConfig.repo == repo)
            )
        ).first()
        return str(row[0]) if row and row[0] else "feature-branch"

    async def _plan_doc_path(self, session: Any, plan_id: Any) -> str | None:
        row = (
            await session.execute(select(Plan.doc_path).where(Plan.id == plan_id))
        ).first()
        return str(row[0]) if row and row[0] else None

    async def _escalate(self, session: Any, task: Task, reason: str) -> None:
        """Persist a durable operator escalation (dashboard escalation bucket). Written directly
        so it lands in record-only mode; shared by the headless-verdict and conflict paths."""
        session.add(
            Event(
                entity_type="task",
                action="escalated_to_operator",
                task_id=task.id,
                payload=encode_payload(
                    TaskEscalatedToOperator(
                        task_id=task.id, repo=task.repo, reason=reason,  # type: ignore[arg-type]
                        created_by=task.created_by,
                    )
                ),
            )
        )
        await session.commit()
        logger.warning(
            "dispatch consumer: escalated task=%s reason=%s (created_by=%s)",
            task.id, reason, task.created_by,
        )

    async def _escalate_undeliverable_verdict(self, session: Any, task: Task) -> None:
        """A verdict we cannot apply (no head_sha, no open PR) is ESCALATED, not dropped to a
        log line — a silent drop would stall the task (Bert review). Delegates to ``_escalate``,
        which persists the durable ``task.escalated_to_operator`` row the dashboard bucket reads."""
        await self._escalate(session, task, "verdict_undeliverable")

    async def _dispatch(self, session: Any, task: Task, *, trigger: str) -> bool:
        """Record ONE author dispatch for ``task`` at its current generation. Returns True if
        a new row was created, False if the unique index no-oped a re-delivery.

        The ``task_executions`` row IS the dispatch record SC2 asserts appears deterministically
        on a delivered event. The worker label comes from the assignment policy
        (``_resolve_worker``): continuity for a rework, else load-balance over the repo's roster.
        """
        worker_label = await self._resolve_worker(session, task)
        try:
            # SAVEPOINT: the partial UNIQUE (task_id, generation) violation surfaces at flush
            # OR at commit; a nested transaction contains it so a re-delivery rolls back to the
            # savepoint WITHOUT poisoning the outer transaction or the loop's other dependents.
            async with session.begin_nested():
                session.add(
                    TaskExecution(
                        task_id=task.id,
                        worker_label=worker_label,
                        trigger=trigger,
                        generation=task.generation,
                    )
                )
                await session.flush()
        except IntegrityError:
            # Already an author dispatch for this cycle — a re-delivered event. Idempotent
            # no-op by construction; the savepoint rolled back, the outer txn is intact.
            logger.debug(
                "dispatch consumer: %s already dispatched at generation %s (re-delivery no-op)",
                task.id,
                task.generation,
            )
            return False
        # LAUNCH: the dispatch row landed — emit task.ready in the SAME transaction so the row
        # and the durable Event commit atomically. The assigned worker (subscribed by label)
        # consumes it and fetches its brief. No dispatcher (dark/test) -> record only, no launch.
        if self._dispatcher is not None:
            await self._dispatcher.persist_and_publish(
                session,
                entity_type="task",
                action="ready",
                payload=TaskReady(),
                task_id=task.id,
            )
        await session.commit()
        logger.info(
            "dispatch consumer: dispatched task=%s generation=%s trigger=%s worker=%s",
            task.id,
            task.generation,
            trigger,
            worker_label,
        )
        return True

    async def _resolve_worker(self, session: Any, task: Task) -> str:
        """Assign the worker for a dispatch: continuity for a rework, else load-balance over the
        repo's roster (``select_worker``). Roster = ``team_configs.worker_labels`` (the persisted
        per-repo team shape, ``worker-<slug>-1..N``). Falls back to a synthetic label when a repo
        has no team config / empty roster — the router is dark without a real team anyway, and
        the label only needs to be deterministic for the dispatch record + idempotency.

        The empty-roster fallback is defensive, not a normal path: plan-submit 412s without a
        ``team_configs`` row and ``team up`` defaults to 3 workers, so a router plan always has a
        roster (an empty ``worker_labels`` needs a deliberate ``team up --workers 0``). Bert #417
        flagged that a synthetic label goes to no live worker → a silent stall on that
        misconfiguration; the roster invariant is why we keep the record here rather than
        escalate. If ``--workers 0`` is ever a real path, switch this to an operator escalation
        (like the headless-verdict fix)."""
        roster = await self._worker_roster(session, task.repo)
        prior = await self._prior_worker(session, str(task.id))
        if not roster:
            logger.warning(
                "dispatch consumer: no worker roster for repo=%s (no team_config); "
                "using synthetic label", task.repo,
            )
            return prior or f"router-worker-{task.repo}"
        load = await self._worker_load(session, task.repo, roster)
        return select_worker(roster, load, prior) or f"router-worker-{task.repo}"

    async def _worker_roster(self, session: Any, repo: str) -> list[str]:
        """The repo's configured worker labels (``team_configs.worker_labels``), or [] if the
        repo has no team config."""
        row = (
            await session.execute(
                select(TeamConfig.worker_labels).where(TeamConfig.repo == repo)
            )
        ).first()
        return list(row[0]) if row and row[0] else []

    async def _prior_worker(self, session: Any, task_id: str) -> str | None:
        """The worker that served the most recent author dispatch of THIS task (any generation),
        for rework continuity. None on a first dispatch."""
        row = (
            await session.execute(
                text(
                    "SELECT worker_label FROM task_executions "
                    "WHERE task_id = :t "
                    "  AND trigger IN ('initial','coordinator-rework','evaluator-rework') "
                    # generation DESC is the deterministic secondary sort: two executions with an
                    # identical started_at (rare) would otherwise tie non-deterministically, so
                    # break on the higher generation — the later rework cycle (Bert #417).
                    "ORDER BY started_at DESC, generation DESC LIMIT 1"
                ),
                {"t": task_id},
            )
        ).first()
        return str(row[0]) if row and row[0] else None

    async def _worker_load(
        self, session: Any, repo: str, roster: list[str]
    ) -> dict[str, int]:
        """In-flight load per roster worker: the count of DISTINCT non-terminal tasks in ``repo``
        each worker is assigned (an author execution exists, and the task has no ``pr_merged``).
        The load-balance input for ``select_worker``; a worker absent from the map has load 0."""
        rows = (
            await session.execute(
                text(
                    "SELECT te.worker_label, count(DISTINCT te.task_id) "
                    "FROM task_executions te JOIN tasks t ON t.id = te.task_id "
                    "WHERE t.repo = :r "
                    "  AND te.trigger IN ('initial','coordinator-rework','evaluator-rework') "
                    "  AND te.worker_label IN :roster "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM events e "
                    "    WHERE e.task_id = t.id AND e.action = 'pr_merged'"
                    "  ) "
                    "GROUP BY te.worker_label"
                ).bindparams(bindparam("roster", expanding=True)),
                {"r": repo, "roster": roster},
            )
        ).all()
        return {str(w): int(n) for w, n in rows}


def make_dispatch_consumer(
    settings: Any, session_factory: Any = None, publisher: Any = None
) -> DispatchConsumer:
    """Build a DispatchConsumer from Settings + the app's EventPublisher. Dark unless
    ``ROUTER_DISPATCH_ENABLED`` is set, mirroring ``make_fabric_event_sink`` — the router ships
    behind a flag so a repo runs the router or the legacy coordinator during cutover (ADR-0118
    rollout). The publisher (``app.state.publisher``) drives the durable task.ready launch.

    ``runner_factory`` is intentionally NOT wired here — approve→integration stays dark until the
    enable step decides the git IDENTITY/creds the API pushes as and provides a working-clone
    state dir + remote URL (then a factory ``repo -> SubprocessGitRunner(ensure_working_clone(...))``
    is passed). Without it the approve path records the approval (the ``verdict_applications``
    work-list) but performs no git operations — the same "wired but off" shape as the launch."""
    enabled = bool(getattr(settings, "router_dispatch_enabled", False))
    dispatcher = Dispatcher(publisher) if publisher is not None else None
    return DispatchConsumer(
        session_factory=session_factory, dispatcher=dispatcher, enabled=enabled
    )
