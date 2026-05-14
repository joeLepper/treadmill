"""Handler-level unit tests for the coordination consumer.

The integration tests in ``test_integration_coordination_consumer.py``
cover the happy paths against live Postgres. These unit tests drive
``CoordinationConsumer.handle()`` against a stub sessionmaker so we can
prove the Pydantic validation gate fires *before* any SQL is issued —
a property that's awkward to demonstrate against a real DB without
intrusive instrumentation.

See work item A.3 in ``docs/plans/2026-05-11-week-2-closure.md``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from treadmill_api.coordination.consumer import CoordinationConsumer


class _StubSession:
    """Records all execute / commit calls so tests can assert which SQL
    statements (if any) were issued."""

    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.commit = AsyncMock()


def _stub_factory(session: _StubSession) -> Any:
    """Build something that behaves like an async_sessionmaker: callable,
    returns an async-context-manager that yields the supplied session."""

    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    def _make() -> Any:
        return _cm()

    return _make


def _consumer(session: _StubSession) -> CoordinationConsumer:
    return CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(session),  # type: ignore[arg-type]
    )


# ── Validation gate: no SQL when payload is malformed ────────────────────────


@pytest.mark.asyncio
async def test_handle_rejects_malformed_started_payload_before_db() -> None:
    """A ``step.started`` with no ``started_at`` field fails Pydantic
    validation in ``parse_payload`` — the handler returns without ever
    opening a session or issuing SQL."""
    session = _StubSession()
    consumer = _consumer(session)

    await consumer.handle({
        "entity_type": "step",
        "action": "started",
        "step_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "payload": {},  # missing required started_at
    })
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_rejects_unknown_event_type_before_db() -> None:
    """``(entity_type, action) = ('step', 'weird')`` is not registered
    in ``EVENT_REGISTRY``; the handler returns without DB work."""
    session = _StubSession()
    consumer = _consumer(session)

    await consumer.handle({
        "entity_type": "step",
        "action": "weird",
        "step_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "payload": {},
    })
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_skips_non_step_entity_without_session() -> None:
    """Non-step entity types short-circuit before the validation gate
    AND before any DB work."""
    session = _StubSession()
    consumer = _consumer(session)

    await consumer.handle({
        "entity_type": "plan",
        "action": "registered",
        "plan_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "payload": {"repo": "x/y"},
    })
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_skips_step_event_without_step_id() -> None:
    """A step event missing ``step_id`` is malformed at the envelope
    level — log + return; no DB work."""
    session = _StubSession()
    consumer = _consumer(session)

    await consumer.handle({
        "entity_type": "step",
        "action": "started",
        "event_id": str(uuid.uuid4()),
        "payload": {"started_at": "2026-05-08T10:00:00+00:00"},
    })
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_validates_and_opens_session_on_well_formed_payload() -> None:
    """When the payload validates, the handler opens a session and issues
    the persist + update statements. We don't assert on the SQL text here
    (that's the integration test's job) — just that DB work happens."""
    session = _StubSession()
    consumer = _consumer(session)

    await consumer.handle({
        "entity_type": "step",
        "action": "started",
        "step_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "payload": {"started_at": "2026-05-08T10:00:00+00:00"},
    })
    # _persist_event (audit INSERT) + _dispatch_step (status UPDATE)
    assert session.execute.await_count == 2
    session.commit.assert_awaited()


# ── github.pr_merged handler (Week 3 B.3) ────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_github_pr_merged_skips_sweep_when_github_client_unwired() -> None:
    """The pr_merged handler validates the payload and then runs two
    side-effects:

      * the Week-3 C.2 trigger evaluator (which runs for every github
        verb; no row in ``event_triggers`` for ``pr_merged`` so it
        no-ops cleanly),
      * the Week-3 B.3 conflict sweep (which short-circuits when
        ``github_client`` is None — GITHUB_TOKEN unset at boot).

    With ``dispatcher`` also None (narrow test), the evaluator is
    skipped before opening any DB cursors. The persist-event INSERT
    still fires (one ``execute``) and the txn commits."""
    session = _StubSession()
    consumer = _consumer(session)
    # ``github_client`` and ``dispatcher`` default to None when not passed.

    await consumer.handle({
        "entity_type": "github",
        "action": "pr_merged",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": "x/y",
            "pr_number": 42,
            "sender": "alice",
            "merged_sha": "deadbeef" * 5,
        },
    })
    # One execute call for the audit-row INSERT; commit fires once.
    assert session.execute.await_count == 1
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_handle_github_pr_merged_runs_reevaluate_for_dependent_tasks() -> None:
    """Per the ADR-0023 smoke (2026-05-13) — a pr_merged event must
    trigger the re-evaluation pass so tasks whose
    ``task.<uuid>.pr_merged`` dependency was just satisfied get
    dispatched. The redispatch module's docstring originally deferred
    this; we wire it here in the consumer.
    """
    session = _StubSession()
    consumer = _consumer(session)

    reevaluate_calls: list[int] = []

    async def _stub_reevaluate() -> None:
        reevaluate_calls.append(1)

    consumer._reevaluate = _stub_reevaluate  # type: ignore[method-assign]

    await consumer.handle({
        "entity_type": "github",
        "action": "pr_merged",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": "x/y",
            "pr_number": 42,
            "sender": "alice",
            "merged_sha": "deadbeef" * 5,
        },
    })

    assert reevaluate_calls == [1], (
        "consumer's pr_merged path must invoke _reevaluate so "
        "dependent tasks can dispatch when their dep is satisfied"
    )


@pytest.mark.asyncio
async def test_reevaluate_dispatches_tasks_returned_by_sql_including_deferred() -> None:
    """Hole 4 (2026-05-13) — ``reevaluate`` must dispatch every task id
    its SELECT returns, including tasks with deferred runs (population 2
    in the docstring). We don't drive live Postgres here — we fake the
    SQL result and verify the dispatcher is called for each id.

    Pairs with ``test_dispatch_task_reuses_deferred_run_*`` in
    ``test_dispatch_unit.py``: that suite covers the reuse semantics on
    the dispatcher side; this test covers the SELECT-then-dispatch loop
    that drives them.
    """
    from unittest.mock import MagicMock, AsyncMock

    from treadmill_api.coordination.redispatch import reevaluate

    deferred_task_id = uuid.uuid4()
    fresh_task_id = uuid.uuid4()

    # Fake session.execute returns two rows (deferred + fresh); session.get
    # returns a Task stub for each.
    class _Row:
        def __init__(self, task_id: uuid.UUID) -> None:
            self.id = task_id

    rows = [_Row(deferred_task_id), _Row(fresh_task_id)]
    session = AsyncMock()
    exec_result = MagicMock()
    exec_result.all.return_value = rows
    session.execute = AsyncMock(return_value=exec_result)

    async def _fake_get(_model: Any, task_id: uuid.UUID) -> Any:
        stub = MagicMock()
        stub.id = task_id
        return stub

    session.get = _fake_get

    dispatched_with: list[uuid.UUID] = []

    class _StubDispatcher:
        async def dispatch_task(self, _session: Any, task: Any) -> uuid.UUID:
            dispatched_with.append(task.id)
            return uuid.uuid4()

    result = await reevaluate(session, _StubDispatcher())
    assert dispatched_with == [deferred_task_id, fresh_task_id]
    assert result == [deferred_task_id, fresh_task_id]


def test_pending_tasks_sql_includes_deferred_run_population() -> None:
    """Hole 4 (2026-05-13) — structural assertion: the SELECT must
    include tasks that have a ``workflow_runs`` row but no
    ``step.ready`` event. A future refactor that quietly drops this
    branch would reintroduce the bug; the test fails fast.
    """
    from treadmill_api.coordination.redispatch import _PENDING_TASKS_SQL

    sql = str(_PENDING_TASKS_SQL)
    assert "workflow_runs" in sql
    assert "step" in sql and "ready" in sql, (
        "the SELECT must reference the step.ready event to identify "
        "the deferred-run population"
    )


@pytest.mark.asyncio
async def test_handle_github_pr_opened_does_not_run_reevaluate() -> None:
    """The reevaluate pass on github events is specific to pr_merged
    (the verb that can satisfy a task.<uuid>.pr_merged dependency).
    Other github verbs do not invoke it."""
    session = _StubSession()
    consumer = _consumer(session)

    reevaluate_calls: list[int] = []

    async def _stub_reevaluate() -> None:
        reevaluate_calls.append(1)

    consumer._reevaluate = _stub_reevaluate  # type: ignore[method-assign]

    await consumer.handle({
        "entity_type": "github",
        "action": "pr_opened",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": "x/y",
            "pr_number": 42,
            "sender": "alice",
            "head_sha": "deadbeef" * 5,
        },
    })

    assert reevaluate_calls == [], (
        "non-pr_merged github verbs do not satisfy pr_merged "
        "dependencies; no reevaluate pass should fire"
    )


@pytest.mark.asyncio
async def test_handle_github_pr_merged_rejects_malformed_payload() -> None:
    """A pr_merged event missing the required ``repo`` field fails the
    Pydantic gate before the handler attempts to open a session or call
    the sweep. Even with both github_client + publisher wired, the
    malformed payload short-circuits."""
    session = _StubSession()
    from unittest.mock import MagicMock

    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(session),  # type: ignore[arg-type]
        github_client=MagicMock(),
        publisher=MagicMock(),
    )
    await consumer.handle({
        "entity_type": "github",
        "action": "pr_merged",
        "event_id": str(uuid.uuid4()),
        "payload": {"pr_number": 42, "sender": "alice"},  # missing repo
    })
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_github_other_actions_persist_audit_row() -> None:
    """The consumer's github branch persists every well-formed github
    event's audit row (Week-3 C.2). With ``dispatcher`` un-wired the
    trigger evaluator skips cleanly — no run-creation SQL fires, but
    the audit INSERT still does (one ``execute``)."""
    session = _StubSession()
    consumer = _consumer(session)

    await consumer.handle({
        "entity_type": "github",
        "action": "pr_opened",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": "x/y",
            "pr_number": 42,
            "sender": "alice",
            "title": "feat: add",
            "head_branch": "task/foo",
            "head_sha": "deadbeef" * 5,
        },
    })
    assert session.execute.await_count == 1
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_handle_github_unknown_action_is_dropped_before_db() -> None:
    """A github verb that isn't in the event registry fails the parse gate
    and never reaches the session — keeps the audit log clean of unparseable
    rows."""
    session = _StubSession()
    consumer = _consumer(session)

    await consumer.handle({
        "entity_type": "github",
        "action": "weird_new_event",
        "event_id": str(uuid.uuid4()),
        "payload": {"repo": "x/y", "pr_number": 42},
    })
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


# ── Poll-loop resilience + health status (A.11) ───────────────────────────────


class _SequencedSqs:
    """Stub SQS whose ``receive_message`` walks a pre-arranged pattern
    of failures and successes — used to drive the consumer's backoff
    path deterministically without spinning up a real broker."""

    def __init__(self, pattern: list[bool]) -> None:
        # Each entry: True = fail, False = succeed (return empty list).
        self.pattern = list(pattern)
        self.call_count = 0

    def receive_message(self, **_kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        if not self.pattern:
            # Out of pattern — keep returning empty so the loop spins
            # benignly until something else stops it.
            return {"Messages": []}
        if self.pattern.pop(0):
            raise RuntimeError(f"flaky sqs at call {self.call_count}")
        return {"Messages": []}


@pytest.mark.asyncio
async def test_run_backs_off_exponentially_on_consecutive_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive ``receive_message`` failures must yield backoff sleeps
    of 1, 2, 4, 8, 16, 30, 30 seconds (the 6th onward saturates at 30).
    The first successful poll resets the counter so the next failure
    starts back at 1s.

    The test patches ``asyncio.sleep`` to a recorder so backoff durations
    are exact and the test runs instantly.
    """
    sleeps: list[float] = []

    session = _StubSession()
    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(session),  # type: ignore[arg-type]
    )

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        # Stop after we've observed 8 backoffs — that's enough to
        # show the saturation cap + the counter reset.
        if len(sleeps) >= 8:
            consumer._stopped = True

    monkeypatch.setattr(
        "treadmill_api.coordination.consumer.asyncio.sleep", _fake_sleep
    )

    # Pattern: 7 fails, 1 success, 1 more fail → expect sleeps
    # 1, 2, 4, 8, 16, 30, 30, then NO sleep on success, then 1 again.
    consumer.sqs = _SequencedSqs(
        [True, True, True, True, True, True, True, False, True]
    )

    await consumer._run()

    # First seven entries are the failing-poll backoffs.
    assert sleeps[:7] == [1, 2, 4, 8, 16, 30, 30]
    # The eighth entry is the post-reset backoff (the success in between
    # reset the counter, so the next failure starts at 1s again).
    assert sleeps[7] == 1


@pytest.mark.asyncio
async def test_run_uses_min_2_pow_cap_at_30(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backoff saturates at 30s for ``failures >= 6`` (2^5 == 32 > 30
    → cap). Explicit assertion so the cap doesn't silently regress."""
    sleeps: list[float] = []

    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(_StubSession()),  # type: ignore[arg-type]
    )

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 8:
            consumer._stopped = True

    monkeypatch.setattr(
        "treadmill_api.coordination.consumer.asyncio.sleep", _fake_sleep
    )

    class _AlwaysFail:
        def receive_message(self, **_k: Any) -> dict[str, Any]:
            raise RuntimeError("always fail")

    consumer.sqs = _AlwaysFail()
    consumer._stopped = False

    await consumer._run()

    # 1, 2, 4, 8, 16, 30, 30, 30
    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30]


def test_health_status_starts_as_starting() -> None:
    """A freshly-constructed consumer reports ``starting`` — no poll has
    completed yet."""
    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(_StubSession()),  # type: ignore[arg-type]
    )
    assert consumer.status_for_health() == "starting"


@pytest.mark.asyncio
async def test_health_status_reflects_consumer_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the consumer through the transition table:

      starting → running → degraded → running

    By feeding it a success-then-failure-then-success pattern.
    """
    sleeps: list[float] = []

    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(_StubSession()),  # type: ignore[arg-type]
    )

    # Recorded health states observed *after* each poll.
    observed: list[str] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        "treadmill_api.coordination.consumer.asyncio.sleep", _fake_sleep
    )

    # 4 polls: success, failure, failure, success, then stop.
    class _Pattern:
        def __init__(self) -> None:
            self.calls = 0

        def receive_message(self, **_k: Any) -> dict[str, Any]:
            self.calls += 1
            # Record the *pre-poll* status only for the first call.
            if self.calls == 1:
                observed.append(consumer.status_for_health())  # starting
                return {"Messages": []}
            if self.calls == 2:
                observed.append(consumer.status_for_health())  # running
                raise RuntimeError("transient")
            if self.calls == 3:
                observed.append(consumer.status_for_health())  # degraded
                raise RuntimeError("transient")
            if self.calls == 4:
                observed.append(consumer.status_for_health())  # degraded
                return {"Messages": []}
            observed.append(consumer.status_for_health())
            consumer._stopped = True
            return {"Messages": []}

    # Initial state — before any poll.
    assert consumer.status_for_health() == "starting"

    consumer.sqs = _Pattern()
    await consumer._run()

    # Final state after the last successful poll is running.
    assert consumer.status_for_health() == "running"

    # The observed-during-poll trail captures the transitions:
    assert observed[0] == "starting"
    assert observed[1] == "running"
    assert observed[2] == "degraded"
    assert observed[3] == "degraded"


@pytest.mark.asyncio
async def test_health_status_dead_when_task_died_unexpectedly() -> None:
    """If ``_run`` exited (task is done) without ``stop()`` flipping
    ``_stopped``, ``status_for_health`` reports ``dead``."""
    import asyncio as _asyncio

    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(_StubSession()),  # type: ignore[arg-type]
    )

    async def _explode() -> None:
        raise RuntimeError("explode immediately")

    consumer._task = _asyncio.create_task(_explode())
    # Let the task run + die.
    try:
        await consumer._task
    except RuntimeError:
        pass

    assert consumer.status_for_health() == "dead"
    assert consumer.is_running() is False
