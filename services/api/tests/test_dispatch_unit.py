"""Unit tests for the dispatcher's no-Request callers + claim body shape.

These exercise ``Dispatcher`` against fake publisher + fake SQS so we
can cover the SQS claim-body extension (B.4 API-side) and the publish-
failure path (the pre-A.8 log-and-continue behavior) without spinning
up the full app or Postgres.

Real DB integration is covered separately in
``test_integration_plans_router.py`` and ``test_integration_routers.py``.

See work items A.7 + B.4 in ``docs/plans/2026-05-11-week-2-closure.md``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.dispatch import Dispatcher, DispatchError
from treadmill_api.models import Task


# ── Test doubles ──────────────────────────────────────────────────────────────


class _FakePublisher:
    """Records ``publish`` calls. Optionally raises on a configured call
    index so we can drive the failure-path test."""

    def __init__(self, *, raise_on_call: int | None = None) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self._raise_on_call = raise_on_call

    async def publish(self, event: Any, payload: Any) -> None:
        if (
            self._raise_on_call is not None
            and len(self.calls) == self._raise_on_call
        ):
            self.calls.append((event, payload))
            raise RuntimeError("simulated publish failure")
        self.calls.append((event, payload))


class _FakeSqs:
    def __init__(self, *, raise_on_send: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self._raise_on_send = raise_on_send

    def send_message(self, **kwargs: Any) -> None:
        if self._raise_on_send:
            raise RuntimeError("simulated SQS send failure")
        self.sent.append(kwargs)


class _Step:
    def __init__(self, *, step_index: int, step_name: str, role_id: str) -> None:
        self.id = uuid.uuid4()
        self.step_index = step_index
        self.step_name = step_name
        self.role_id = role_id


class _FakeSession:
    """Minimal async-session stub.

    The dispatcher's ``dispatch_task`` exercises ``execute``, ``get``,
    ``add``, and ``flush`` on the session. ``execute`` dispatches on the
    SQL statement's shape so we can serve the multiple selects the
    dispatcher now issues (workflow steps, idempotency probe, plan-active
    gate, task_dependencies) without standing up Postgres.

    Side-effect: every ``add(WorkflowRunStep)`` populates a server-default
    id so subsequent code sees a valid UUID — matching what asyncpg
    would produce on flush.

    Gate flags
    ----------

    ``plan_active`` controls what the ``plan_status`` VIEW lookup
    returns — defaults to ``True`` so legacy tests still see a happy
    dispatch. Set to ``False`` to exercise the D.5 plan-active gate's
    deferred-dispatch path.

    ``dependencies`` is a list of ``(expression, satisfied)`` tuples
    that the ``task_dependencies`` select returns. Defaults to empty so
    legacy tests dispatch as before.
    """

    def __init__(
        self,
        *,
        wv_steps: list[Any],
        workflow_version: Any,
        plan_active: bool = True,
        dependency_expressions: list[str] | None = None,
        existing_step_ready_run_id: uuid.UUID | None = None,
        existing_deferred_run_id: uuid.UUID | None = None,
        existing_deferred_run_steps: list[Any] | None = None,
        dep_pr_merged_count: int = 0,
    ) -> None:
        self.added: list[Any] = []
        self._wv_steps = wv_steps
        self._workflow_version = workflow_version
        self._plan_active = plan_active
        self._dep_expressions = list(dependency_expressions or [])
        self._existing_run_id = existing_step_ready_run_id
        self._existing_deferred_run_id = existing_deferred_run_id
        self._existing_deferred_run_steps = list(existing_deferred_run_steps or [])
        self._dep_pr_merged_count = dep_pr_merged_count
        self.flushed = 0

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        """Dispatch on the statement shape — keyed on the column/table
        referenced. Done with string inspection of the compiled SQL: the
        dispatcher uses both SQLAlchemy Core selects and a ``text()`` for
        the plan_status VIEW lookup, and this fake handles both."""
        compiled = str(stmt)
        result = MagicMock()
        if "plan_status" in compiled:
            # is_plan_active() — VIEW lookup
            row = MagicMock()
            row.derived_status = "active" if self._plan_active else "drafting"
            result.first.return_value = row
            return result
        if "task_dependencies" in compiled:
            # evaluate_dependencies() — list of expression strings
            result.scalars.return_value.all.return_value = list(self._dep_expressions)
            return result
        if "count" in compiled.lower() and "events" in compiled:
            # _is_dep_pr_merged() — count(events). Configurable so we can
            # exercise the deferred→satisfied transition at the unit level.
            result.scalar_one.return_value = self._dep_pr_merged_count
            return result
        if "count" in compiled.lower() and "workflow_run_steps" in compiled:
            # _is_dep_step_completed() — count(workflow_run_steps).
            result.scalar_one.return_value = 0
            return result
        if "workflow_run_steps" in compiled and "workflow_runs" not in compiled:
            # _find_deferred_run reuse path fetches the existing steps
            # via SELECT WorkflowRunStep WHERE run_id = ...
            result.scalars.return_value = iter(self._existing_deferred_run_steps)
            return result
        if "workflow_runs" in compiled and "workflow_run_steps" not in compiled:
            # Two queries hit this branch:
            #   * _is_dep_run_completed() — .scalars().all() of runs
            #   * _find_deferred_run() — .scalar_one_or_none() of single id
            # We populate both result-extractors on the same mock so the
            # caller picks the shape they expect.
            result.scalars.return_value.all.return_value = []
            result.scalar_one_or_none.return_value = self._existing_deferred_run_id
            return result
        if "FROM events" in compiled or "events.run_id" in compiled or "events.task_id" in compiled:
            # _has_step_ready_event() — single run_id or None
            result.scalar_one_or_none.return_value = self._existing_run_id
            return result
        # Default: the workflow-version-steps select.
        result.scalars.return_value = iter(self._wv_steps)
        return result

    async def get(self, model: Any, _id: Any) -> Any:
        # The dispatcher's deferred-run reuse path calls
        # ``session.get(WorkflowRun, deferred_run_id)`` to load the
        # existing run; default-construct a stand-in so the caller can
        # read ``.id`` off it. Everything else maps to the workflow
        # version stub.
        if getattr(model, "__name__", "") == "WorkflowRun":
            stub = MagicMock()
            stub.id = self._existing_deferred_run_id
            return stub
        return self._workflow_version

    def add(self, obj: Any) -> None:
        # The dispatcher creates WorkflowRun + WorkflowRunStep + Event;
        # synthesize ids the way Postgres would.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


def _task(*, workflow_version_id: uuid.UUID | None = None) -> Task:
    """Build a Task instance with the SQL defaults filled in."""
    t = Task(
        plan_id=uuid.uuid4(),
        repo="test/repo",
        title="A task",
        description=None,
        workflow_version_id=workflow_version_id or uuid.uuid4(),
        created_by=None,
    )
    t.id = uuid.uuid4()
    return t


# ── from_app_state ────────────────────────────────────────────────────────────


def test_from_app_state_pulls_publisher_and_sqs_and_queue_url() -> None:
    """``Dispatcher.from_app_state`` mirrors the FastAPI-dependency
    constructor — background callers (replay loop, re-evaluation pass)
    instantiate dispatchers without a Request via this seam."""
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    state = MagicMock()
    state.publisher = publisher
    state.sqs_client = sqs
    state.settings.work_queue_url = "https://sqs.example.com/work"

    d = Dispatcher.from_app_state(state)
    assert d.publisher is publisher
    assert d.sqs_client is sqs
    assert d.work_queue_url == "https://sqs.example.com/work"


def test_from_app_state_tolerates_missing_sqs_client() -> None:
    """In test contexts the lifespan may not populate ``sqs_client``;
    ``from_app_state`` defaults to ``None``."""

    class _BareState:
        def __init__(self) -> None:
            self.publisher = _FakePublisher()
            self.settings = MagicMock()
            self.settings.work_queue_url = None

    d = Dispatcher.from_app_state(_BareState())
    assert d.sqs_client is None
    assert d.work_queue_url is None


# ── dispatch_task — happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_task_happy_path_produces_event_publish_send() -> None:
    """One Event row (step.ready), one publish call, one SQS send. The
    SQS body carries all four IDs (B.4 — claim-body extension)."""
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
    )
    task = _task()

    run_id = await d.dispatch_task(session, task)  # type: ignore[arg-type]

    # 1 Event row (step.ready)
    event_rows = [
        x for x in session.added
        if type(x).__name__ == "Event" and x.action == "ready"
    ]
    assert len(event_rows) == 1
    # 1 publish
    assert len(publisher.calls) == 1
    # 1 SQS send
    assert len(sqs.sent) == 1
    body = json.loads(sqs.sent[0]["MessageBody"])
    # B.4 — all four IDs present in the claim body.
    assert set(body.keys()) == {"step_id", "task_id", "plan_id", "run_id"}
    assert body["task_id"] == str(task.id)
    assert body["plan_id"] == str(task.plan_id)
    assert body["run_id"] == str(run_id)
    assert sqs.sent[0]["MessageGroupId"] == str(run_id)


# ── dispatch_task — failure paths ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_task_raises_dispatch_error_when_workflow_has_no_steps() -> None:
    """A workflow version with no steps is a misconfigured workflow —
    raised as ``DispatchError`` for the HTTP layer to map to 400."""
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    session = _FakeSession(wv_steps=[], workflow_version=MagicMock())
    task = _task()
    with pytest.raises(DispatchError):
        await d.dispatch_task(session, task)  # type: ignore[arg-type]
    # No publish, no SQS send.
    assert publisher.calls == []
    assert sqs.sent == []


@pytest.mark.asyncio
async def test_dispatch_task_returns_run_id_when_publish_fails() -> None:
    """Pre-A.8 behavior: a publisher exception is logged and swallowed —
    the task creation still succeeds and ``dispatch_task`` returns the
    run id so the HTTP path returns 201. Phase 3 A.8 will add the
    durable ``dispatch_publish_failed`` marker."""
    publisher = _FakePublisher(raise_on_call=0)
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
    )
    task = _task()
    # Should not raise — the failure is logged + swallowed.
    run_id = await d.dispatch_task(session, task)  # type: ignore[arg-type]
    assert run_id is not None
    # Despite the publish exception, the SQS send still went through.
    assert len(sqs.sent) == 1


@pytest.mark.asyncio
async def test_dispatch_claim_body_contains_all_ids() -> None:
    """Explicit B.4 contract test — the four IDs the worker reads from
    the claim body are all present, with the right values."""
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    wv_step = MagicMock(
        step_index=0, step_name="author", role_id="role-author",
        workflow_version_id=uuid.uuid4(),
    )
    session = _FakeSession(wv_steps=[wv_step], workflow_version=wv)
    task = _task()
    await d.dispatch_task(session, task)  # type: ignore[arg-type]

    body = json.loads(sqs.sent[0]["MessageBody"])
    assert body["task_id"] == str(task.id)
    assert body["plan_id"] == str(task.plan_id)
    # run_id and step_id are UUIDs — just check they parse.
    uuid.UUID(body["run_id"])
    uuid.UUID(body["step_id"])


# ── persist_and_publish ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_and_publish_returns_event_row() -> None:
    """The helper INSERTs an Event row, flushes, and publishes — used by
    the plans + tasks routers for lifecycle events (A.6)."""
    from treadmill_api.events.plan import PlanRegistered

    publisher = _FakePublisher()
    d = Dispatcher(
        publisher=publisher, sqs_client=None, work_queue_url=None,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    payload = PlanRegistered(repo="test/repo", intent="x")
    plan_id = uuid.uuid4()
    event = await d.persist_and_publish(
        session, entity_type="plan", action="registered",
        payload=payload, plan_id=plan_id,
    )
    assert event.entity_type == "plan"
    assert event.action == "registered"
    assert event.plan_id == plan_id
    # The publisher saw the event with the typed payload.
    assert len(publisher.calls) == 1


@pytest.mark.asyncio
async def test_persist_and_publish_swallows_publish_failure() -> None:
    """Pre-A.8 — log + continue. The Event row is still persisted; only
    the bus side fails."""
    from treadmill_api.events.plan import PlanRegistered

    publisher = _FakePublisher(raise_on_call=0)
    d = Dispatcher(
        publisher=publisher, sqs_client=None, work_queue_url=None,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    payload = PlanRegistered(repo="test/repo", intent="x")
    # No raise.
    event = await d.persist_and_publish(
        session, entity_type="plan", action="registered",
        payload=payload, plan_id=uuid.uuid4(),
    )
    assert event is not None


# ── A.8 — DispatchPublishFailed marker on publish/send failure ────────────────


@pytest.mark.asyncio
async def test_dispatch_records_publish_failed_event_when_sns_raises() -> None:
    """A.8 — when the SNS publish raises during ``dispatch_task``, the
    dispatcher persists a ``_internal.dispatch_publish_failed`` Event row
    referencing the original step.ready event id. The replay loop (A.10)
    reads these markers and re-publishes on a slow tick."""
    publisher = _FakePublisher(raise_on_call=0)
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
    )
    task = _task()
    await d.dispatch_task(session, task)  # type: ignore[arg-type]

    # The original step.ready event row was persisted.
    step_ready_rows = [
        x for x in session.added
        if type(x).__name__ == "Event"
        and x.entity_type == "step" and x.action == "ready"
    ]
    assert len(step_ready_rows) == 1
    original_id = step_ready_rows[0].id

    # A DispatchPublishFailed marker exists and references the original.
    marker_rows = [
        x for x in session.added
        if type(x).__name__ == "Event"
        and x.entity_type == "_internal"
        and x.action == "dispatch_publish_failed"
    ]
    assert len(marker_rows) == 1
    marker = marker_rows[0]
    assert marker.payload["original_event_id"] == str(original_id)
    assert marker.payload["target"] == "sns"
    assert "simulated publish failure" in marker.payload["error_message"]
    # The marker is correlated to the originating task/run/step.
    assert marker.task_id == task.id
    assert marker.plan_id == task.plan_id
    assert marker.run_id == step_ready_rows[0].run_id


@pytest.mark.asyncio
async def test_dispatch_records_publish_failed_event_when_sqs_raises() -> None:
    """A.8 — when the SQS ``send_message`` raises, the dispatcher persists
    a ``_internal.dispatch_publish_failed`` marker with ``target='sqs'``.
    The replay loop re-issues against the work queue using the original
    event payload."""
    publisher = _FakePublisher()
    sqs = _FakeSqs(raise_on_send=True)
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
    )
    task = _task()
    await d.dispatch_task(session, task)  # type: ignore[arg-type]

    # SNS publish succeeded (publisher.calls has the step.ready),
    # but a marker still landed for the SQS failure.
    assert len(publisher.calls) == 1
    marker_rows = [
        x for x in session.added
        if type(x).__name__ == "Event"
        and x.entity_type == "_internal"
        and x.action == "dispatch_publish_failed"
    ]
    assert len(marker_rows) == 1
    marker = marker_rows[0]
    assert marker.payload["target"] == "sqs"
    assert "simulated SQS send failure" in marker.payload["error_message"]
    step_ready_rows = [
        x for x in session.added
        if type(x).__name__ == "Event" and x.action == "ready"
    ]
    assert marker.payload["original_event_id"] == str(step_ready_rows[0].id)


@pytest.mark.asyncio
async def test_dispatch_returns_run_id_even_when_publish_fails() -> None:
    """A.8 — both SNS publish and SQS send raising must not prevent the
    dispatcher from returning the run id; the HTTP layer must still
    surface 201. Two markers land (one per failure path)."""
    publisher = _FakePublisher(raise_on_call=0)
    sqs = _FakeSqs(raise_on_send=True)
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
    )
    task = _task()
    run_id = await d.dispatch_task(session, task)  # type: ignore[arg-type]
    assert run_id is not None
    # Two markers — one SNS, one SQS — both reference the same step.ready event.
    targets = sorted(
        x.payload["target"] for x in session.added
        if type(x).__name__ == "Event"
        and x.entity_type == "_internal"
        and x.action == "dispatch_publish_failed"
    )
    assert targets == ["sns", "sqs"]


# ── D.2 / D.5 / idempotency unit tests against the fake session ──────────────


@pytest.mark.asyncio
async def test_dispatch_task_skips_publish_when_plan_not_active() -> None:
    """D.5 — drafting/planning plan: persist run + steps, no publish, no send."""
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
        plan_active=False,
    )
    task = _task()
    run_id = await d.dispatch_task(session, task)  # type: ignore[arg-type]
    assert run_id is not None

    # Run + step rows persisted, but no Event row for step.ready and no
    # publish/send went out.
    runs = [x for x in session.added if type(x).__name__ == "WorkflowRun"]
    steps = [x for x in session.added if type(x).__name__ == "WorkflowRunStep"]
    assert len(runs) == 1
    assert len(steps) == 1
    step_ready_events = [
        x for x in session.added
        if type(x).__name__ == "Event" and x.action == "ready"
    ]
    assert step_ready_events == []
    assert publisher.calls == []
    assert sqs.sent == []


@pytest.mark.asyncio
async def test_dispatch_task_skips_publish_when_dependency_unsatisfied() -> None:
    """D.2 — a ``task_dependencies`` row that evaluates to false defers
    dispatch: run + steps persisted, no publish, no send."""
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    # Construct a dep expression referencing a UUID we will never satisfy.
    sibling_uuid = uuid.uuid4()
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
        plan_active=True,
        dependency_expressions=[f"task.{sibling_uuid}.pr_merged"],
    )
    task = _task()
    await d.dispatch_task(session, task)  # type: ignore[arg-type]

    runs = [x for x in session.added if type(x).__name__ == "WorkflowRun"]
    assert len(runs) == 1
    step_ready = [
        x for x in session.added
        if type(x).__name__ == "Event" and x.action == "ready"
    ]
    assert step_ready == []
    assert publisher.calls == []
    assert sqs.sent == []


@pytest.mark.asyncio
async def test_dispatch_task_idempotent_when_step_ready_already_exists() -> None:
    """Re-entry from the consumer's re-evaluation pass (D.6 hand-off):
    a task with an existing step.ready event short-circuits to its
    existing run id without any further side effects."""
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    existing_run_id = uuid.uuid4()
    session = _FakeSession(
        wv_steps=[],  # would normally raise DispatchError — but idempotency runs first
        workflow_version=MagicMock(),
        existing_step_ready_run_id=existing_run_id,
    )
    task = _task()
    returned = await d.dispatch_task(session, task)  # type: ignore[arg-type]
    assert returned == existing_run_id
    # No side effects.
    assert publisher.calls == []
    assert sqs.sent == []
    assert session.added == []


@pytest.mark.asyncio
async def test_dispatch_task_malformed_dependency_blocks_dispatch() -> None:
    """A row that doesn't parse against the v0 grammar is treated as
    unsatisfied (with a WARNING log). The dispatcher must not leak work
    on bad data — operator-fix-then-re-evaluate is the recovery path."""
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
        plan_active=True,
        dependency_expressions=["totally bogus expression"],
    )
    task = _task()
    await d.dispatch_task(session, task)  # type: ignore[arg-type]
    assert publisher.calls == []
    assert sqs.sent == []


# ── Hole 4 (2026-05-13) — deferred-run re-dispatch ────────────────────────────
#
# When ``dispatch_task`` hits the D.2 / D.5 deferred path it persists a
# ``WorkflowRun`` + steps but does not publish step.ready. The consumer's
# re-evaluation pass calls back into ``dispatch_task`` when an event that
# might satisfy the gate lands. Two properties matter:
#
#   * On the re-call with deps still unsatisfied, NO duplicate run is
#     created (the helper finds the existing deferred run and reuses it).
#   * On the re-call once deps are satisfied, the existing run is the one
#     that gets step.ready emitted (no duplicate).
#
# Both are unit-level — the integration suite exercises the round-trip
# through Postgres + the redispatch SELECT in
# ``test_integration_redispatch.py``.


@pytest.mark.asyncio
async def test_dispatch_task_reuses_deferred_run_when_deps_still_unsatisfied() -> None:
    """Hole 4 (2026-05-13) — calling ``dispatch_task`` twice on a task
    whose dependency is unsatisfied both times must NOT create a duplicate
    ``WorkflowRun``. The second call finds the run from the first call
    and reuses it.
    """
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    sibling_uuid = uuid.uuid4()
    # First call: no deferred run exists yet — the dispatcher creates one.
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
        plan_active=True,
        dependency_expressions=[f"task.{sibling_uuid}.pr_merged"],
    )
    task = _task()
    first_run_id = await d.dispatch_task(session, task)  # type: ignore[arg-type]
    runs_after_first = [
        x for x in session.added if type(x).__name__ == "WorkflowRun"
    ]
    assert len(runs_after_first) == 1
    assert publisher.calls == []
    assert sqs.sent == []

    # Second call simulates the re-evaluation pass: pre-populate the
    # fake session as if the deferred run from the first call were
    # already persisted, and call ``dispatch_task`` again with deps
    # still unsatisfied. No duplicate run must result.
    deferred_run_steps = [
        MagicMock(
            id=uuid.uuid4(),
            step_index=0,
            step_name="author",
            role_id="role-author",
        )
    ]
    session2 = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
        plan_active=True,
        dependency_expressions=[f"task.{sibling_uuid}.pr_merged"],
        existing_deferred_run_id=first_run_id,
        existing_deferred_run_steps=deferred_run_steps,
    )
    second_run_id = await d.dispatch_task(session2, task)  # type: ignore[arg-type]
    assert second_run_id == first_run_id
    runs_after_second = [
        x for x in session2.added if type(x).__name__ == "WorkflowRun"
    ]
    assert runs_after_second == [], (
        "deferred-run reuse must not create a duplicate WorkflowRun "
        "on the re-dispatch call when the gate is still failing"
    )
    # Still no publish — gate still failing.
    assert publisher.calls == []
    assert sqs.sent == []


@pytest.mark.asyncio
async def test_dispatch_task_reuses_deferred_run_and_emits_when_deps_satisfied() -> None:
    """Hole 4 (2026-05-13) — when the re-evaluation pass calls
    ``dispatch_task`` after the satisfying event (``pr_merged``) lands,
    the dispatcher finds the deferred run, reuses it, and emits
    ``step.ready`` against its first step. No duplicate run.
    """
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    wv = MagicMock()
    wv.workflow_id = "wf-author"
    sibling_uuid = uuid.uuid4()
    deferred_run_id = uuid.uuid4()
    deferred_step = MagicMock(
        id=uuid.uuid4(),
        step_index=0,
        step_name="author",
        role_id="role-author",
    )
    # Pre-state: a prior deferred dispatch already persisted the run +
    # step. The pr_merged event has now landed (dep_pr_merged_count=1)
    # so the gate should pass on this call.
    session = _FakeSession(
        wv_steps=[
            MagicMock(
                step_index=0, step_name="author", role_id="role-author",
                workflow_version_id=uuid.uuid4(),
            )
        ],
        workflow_version=wv,
        plan_active=True,
        dependency_expressions=[f"task.{sibling_uuid}.pr_merged"],
        existing_deferred_run_id=deferred_run_id,
        existing_deferred_run_steps=[deferred_step],
        dep_pr_merged_count=1,
    )
    task = _task()
    returned_run_id = await d.dispatch_task(session, task)  # type: ignore[arg-type]

    # The returned run id is the deferred one — not a fresh uuid.
    assert returned_run_id == deferred_run_id
    # No new WorkflowRun in session.added — the existing one was reused.
    runs_added = [x for x in session.added if type(x).__name__ == "WorkflowRun"]
    assert runs_added == []
    # No new WorkflowRunStep rows either — the existing steps were reused.
    steps_added = [
        x for x in session.added if type(x).__name__ == "WorkflowRunStep"
    ]
    assert steps_added == []
    # step.ready emitted exactly once with run_id == deferred_run_id.
    step_ready_events = [
        x for x in session.added
        if type(x).__name__ == "Event" and x.action == "ready"
    ]
    assert len(step_ready_events) == 1
    assert step_ready_events[0].run_id == deferred_run_id
    assert len(publisher.calls) == 1
    # SQS claim body references the deferred run.
    assert len(sqs.sent) == 1
    body = json.loads(sqs.sent[0]["MessageBody"])
    assert body["run_id"] == str(deferred_run_id)
    assert body["step_id"] == str(deferred_step.id)


@pytest.mark.asyncio
async def test_dispatch_task_idempotency_probe_still_short_circuits_with_deferred_run() -> None:
    """The idempotency probe (``_has_step_ready_event``) must still win
    over the deferred-run reuse path. A task with both a deferred run
    *and* a step.ready event (e.g. a successful re-dispatch landed and
    then the consumer re-fires) short-circuits to the step.ready event's
    run id without any further side effects.
    """
    publisher = _FakePublisher()
    sqs = _FakeSqs()
    d = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    existing_run_id = uuid.uuid4()
    other_deferred_id = uuid.uuid4()
    session = _FakeSession(
        wv_steps=[],  # would raise DispatchError if probe didn't fire first
        workflow_version=MagicMock(),
        existing_step_ready_run_id=existing_run_id,
        existing_deferred_run_id=other_deferred_id,  # ignored — probe wins
    )
    task = _task()
    returned = await d.dispatch_task(session, task)  # type: ignore[arg-type]
    assert returned == existing_run_id
    assert publisher.calls == []
    assert sqs.sent == []
    assert session.added == []
