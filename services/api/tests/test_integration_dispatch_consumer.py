"""Real-DB foils for the ADR-0118 event->dispatch consumer + reconcile sweep.

Exercises the routing WIRING against a live Postgres (the schema — the (task_id, generation)
partial unique index and the plans.substrate check — is what makes idempotency + SC6 real):

  * pr_merged with satisfied deps -> the dependent gets exactly ONE author task_execution.
  * re-delivery -> still ONE row (the unique index no-ops the second).
  * legacy-substrate plan -> NO dispatch (SC6).
  * deps NOT satisfied -> NO dispatch.
  * RECONCILE FOIL (the durability proof, per alan): U unblocks [A,B,C]; the live path
    dispatches only A (simulated crash before B,C); reconcile() then dispatches B+C at their
    current generation and does NOT re-dispatch A.

Run:  TREADMILL_INTEGRATION=1 TREADMILL_TEST_DATABASE_URL=<dedicated test db> \
      uv run pytest tests/test_integration_dispatch_consumer.py

NOTE (bert): written to the visible consumer API (DispatchConsumer.handle / .reconcile,
plans.substrate, the author-trigger unique index) but NOT run in this session — it needs the
integration DB. Alan: run it in the integration env and reconcile any consumer-API drift
(esp. the exact dispatch record columns and the ci_result branch once wired).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason="set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL (truncates tables)",
)

REPO = "joeLepper/treadmill"
_TABLES = "plans, tasks, task_prs, task_dependencies, task_executions, evaluator_dispatches, events"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    import subprocess
    from pathlib import Path

    services_api_dir = Path(__file__).resolve().parent.parent
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env={**os.environ, "DATABASE_URL": TEST_DB_URL},
        check=True,
    )
    eng = sa.create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


def _async_maker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = TEST_DB_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://")
    return async_sessionmaker(create_async_engine(url), expire_on_commit=False)


def _truncate(conn):
    conn.execute(sa.text(f"TRUNCATE {_TABLES} CASCADE"))


def _seed_plan(conn, *, substrate: str) -> uuid.UUID:
    pid = uuid.uuid4()
    conn.execute(
        sa.text("INSERT INTO plans (id, repo, intent, substrate) VALUES (:p,:r,'t',:s)"),
        {"p": pid, "r": REPO, "s": substrate},
    )
    return pid


def _seed_task(conn, plan_id: uuid.UUID) -> uuid.UUID:
    tid = uuid.uuid4()
    conn.execute(
        sa.text("INSERT INTO tasks (id, plan_id, repo, title) VALUES (:t,:p,:r,'x')"),
        {"t": tid, "p": plan_id, "r": REPO},
    )
    return tid


def _add_dep(conn, dependent: uuid.UUID, upstream: uuid.UUID) -> None:
    conn.execute(
        sa.text("INSERT INTO task_dependencies (task_id, expression) VALUES (:t, :e)"),
        {"t": dependent, "e": f"task.{upstream}.pr_merged"},
    )


def _mark_pr_merged(conn, upstream: uuid.UUID) -> None:
    """The upstream terminal fact the dependents' edges require."""
    conn.execute(
        sa.text(
            "INSERT INTO events (entity_type, action, task_id, commit_sha, payload) "
            "VALUES ('github','pr_merged',:t,:sha,'{}'::jsonb)"
        ),
        {"t": upstream, "sha": "cafe" + uuid.uuid4().hex[:36]},
    )


def _author_execs(conn, task_id: uuid.UUID) -> int:
    return conn.execute(
        sa.text(
            "SELECT count(*) FROM task_executions WHERE task_id=:t "
            "AND trigger IN ('initial','coordinator-rework','evaluator-rework')"
        ),
        {"t": task_id},
    ).scalar_one()


def _pr_merged_record(task_id: uuid.UUID, plan_id: uuid.UUID) -> dict:
    return {
        "entity_type": "github",
        "action": "pr_merged",
        "task_id": str(task_id),
        "plan_id": str(plan_id),
        "payload": {},
    }


def _consumer():
    from treadmill_api.coordination.dispatch_consumer import DispatchConsumer

    return DispatchConsumer(session_factory=_async_maker(), enabled=True)


@pytest.mark.asyncio
async def test_pr_merged_dispatches_dependent_once_and_redelivery_no_dups(engine: Engine):
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="router")
        upstream = _seed_task(conn, plan)
        dependent = _seed_task(conn, plan)
        _add_dep(conn, dependent, upstream)
        _mark_pr_merged(conn, upstream)

    consumer = _consumer()
    rec = _pr_merged_record(upstream, plan)
    await consumer.handle(rec)
    await consumer.handle(rec)  # re-delivery

    with engine.begin() as conn:
        assert _author_execs(conn, dependent) == 1  # exactly one, despite two deliveries


@pytest.mark.asyncio
async def test_legacy_substrate_is_not_dispatched(engine: Engine):
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="legacy")
        upstream = _seed_task(conn, plan)
        dependent = _seed_task(conn, plan)
        _add_dep(conn, dependent, upstream)
        _mark_pr_merged(conn, upstream)

    await _consumer().handle(_pr_merged_record(upstream, plan))
    with engine.begin() as conn:
        assert _author_execs(conn, dependent) == 0  # SC6: legacy coordinator owns it


@pytest.mark.asyncio
async def test_unsatisfied_dependent_is_not_dispatched(engine: Engine):
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="router")
        upstream = _seed_task(conn, plan)
        other = _seed_task(conn, plan)  # a SECOND edge, never merged
        dependent = _seed_task(conn, plan)
        _add_dep(conn, dependent, upstream)
        _add_dep(conn, dependent, other)  # depends on BOTH
        _mark_pr_merged(conn, upstream)  # only upstream merged -> not satisfied

    await _consumer().handle(_pr_merged_record(upstream, plan))
    with engine.begin() as conn:
        assert _author_execs(conn, dependent) == 0


@pytest.mark.asyncio
async def test_reconcile_backfills_a_crashed_dispatch(engine: Engine):
    """Alan's durability foil. U unblocks [A,B,C]; the live path dispatches ONLY A (crash
    before B,C — we just never deliver their wake); reconcile() then dispatches B+C at their
    current generation and does NOT re-dispatch A."""
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="router")
        upstream = _seed_task(conn, plan)
        a = _seed_task(conn, plan)
        b = _seed_task(conn, plan)
        c = _seed_task(conn, plan)
        for dep in (a, b, c):
            _add_dep(conn, dep, upstream)
        _mark_pr_merged(conn, upstream)
        # Simulate the live path having dispatched ONLY A before the crash.
        conn.execute(
            sa.text(
                "INSERT INTO task_executions (task_id, worker_label, trigger, generation) "
                "VALUES (:t, 'w', 'initial', 1)"
            ),
            {"t": a},
        )

    consumer = _consumer()
    async with _async_maker()() as session:
        dispatched = await consumer.reconcile(session)

    with engine.begin() as conn:
        assert _author_execs(conn, a) == 1  # NOT re-dispatched (unique index)
        assert _author_execs(conn, b) == 1  # backfilled
        assert _author_execs(conn, c) == 1  # backfilled
    assert dispatched == 2  # B and C, not A


# ── ci_result -> evaluator dedup (trailing-suite foil, per alan) ───────────────


def _seed_ci_result(conn, task_id, head, *, app_slug, conclusion):
    """A task.ci_result event is_ci_ready reads (by commit_sha == head)."""
    conn.execute(
        sa.text(
            "INSERT INTO events (entity_type, action, task_id, commit_sha, payload) "
            "VALUES ('task','ci_result',:t,:h,CAST(:p AS jsonb))"
        ),
        {
            "t": task_id,
            "h": head,
            "p": f'{{"app_slug":"{app_slug}","conclusion":"{conclusion}"}}',
        },
    )


def _eval_dispatches(conn, task_id) -> int:
    return conn.execute(
        sa.text("SELECT count(*) FROM evaluator_dispatches WHERE task_id=:t"),
        {"t": task_id},
    ).scalar_one()


def _ci_result_record(task_id, plan_id, head) -> dict:
    return {
        "entity_type": "task",
        "action": "ci_result",
        "task_id": str(task_id),
        "plan_id": str(plan_id),
        "payload": {"head_sha": head},
    }


@pytest.mark.asyncio
async def test_ci_result_fires_evaluator_once_then_dedups_new_head_reevaluates(engine: Engine):
    """Alan's trailing-suite foil against evaluator_dispatches UNIQUE(task_id, head_sha):
    (1) the first ci_result whose required suite is terminal+passing → ONE evaluator dispatch;
    (2) a LATER (trailing non-required) ci_result for the SAME head → is_ci_ready still True,
        but the unique guard no-ops the second → still ONE row;
    (3) a rework push (new head) → a NEW evaluator dispatch.
    """
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="router")
        task = _seed_task(conn, plan)
        # Required suite green at head H1 -> is_ci_ready(H1) is True.
        _seed_ci_result(conn, task, "H1", app_slug="github-actions", conclusion="success")

    consumer = _consumer()
    # (1) first ready ci_result fires the evaluator once.
    await consumer.handle(_ci_result_record(task, plan, "H1"))
    with engine.begin() as conn:
        assert _eval_dispatches(conn, task) == 1

    # (2) a trailing NON-required suite completes for the SAME head; is_ci_ready still True
    #     (it keys on the required github-actions success), but the unique guard no-ops.
    with engine.begin() as conn:
        _seed_ci_result(conn, task, "H1", app_slug="kodiak", conclusion="neutral")
    await consumer.handle(_ci_result_record(task, plan, "H1"))
    with engine.begin() as conn:
        assert _eval_dispatches(conn, task) == 1  # still one — dedup on (task, head)

    # (3) rework push: a new head H2 goes green -> a NEW evaluation.
    with engine.begin() as conn:
        _seed_ci_result(conn, task, "H2", app_slug="github-actions", conclusion="success")
    await consumer.handle(_ci_result_record(task, plan, "H2"))
    with engine.begin() as conn:
        assert _eval_dispatches(conn, task) == 2  # H1 and H2 — distinct evaluations


# ── launch signal: task.ready is emitted on dispatch (alan) ───────────────────


class _StubDispatcher:
    """Records persist_and_publish calls in place of the real Dispatcher (no Event row)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, str]] = []

    async def persist_and_publish(
        self, session, *, entity_type, action, payload, task_id=None, **kw
    ):
        self.published.append((entity_type, action, str(task_id)))
        return None


@pytest.mark.asyncio
async def test_dispatch_emits_task_ready_launch_once(engine: Engine):
    from treadmill_api.coordination.dispatch_consumer import DispatchConsumer

    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="router")
        upstream = _seed_task(conn, plan)
        dependent = _seed_task(conn, plan)
        _add_dep(conn, dependent, upstream)
        _mark_pr_merged(conn, upstream)

    stub = _StubDispatcher()
    consumer = DispatchConsumer(
        session_factory=_async_maker(), dispatcher=stub, enabled=True
    )
    await consumer.handle(_pr_merged_record(upstream, plan))
    # the dependent was dispatched -> a task.ready LAUNCH was emitted for it.
    assert ("task", "ready", str(dependent)) in stub.published
    # re-delivery: idempotent dispatch no-ops BEFORE the publish -> no second launch.
    await consumer.handle(_pr_merged_record(upstream, plan))
    assert stub.published.count(("task", "ready", str(dependent))) == 1


# ── worker dispatch sink: deliver router task.ready to the assigned worker (alan) ──


class _StubHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return None


def _seed_execution(conn, task_id, worker_label, trigger="initial", generation=1):
    conn.execute(
        sa.text(
            "INSERT INTO task_executions (task_id, worker_label, trigger, generation) "
            "VALUES (:t,:w,:tr,:g)"
        ),
        {"t": task_id, "w": worker_label, "tr": trigger, "g": generation},
    )


def _task_ready_record(task_id):
    return {"entity_type": "task", "action": "ready", "task_id": str(task_id), "payload": {}}


def _sink(http):
    from treadmill_api.coordination.worker_dispatch_sink import WorkerDispatchSink

    return WorkerDispatchSink(
        ingress_url="http://ingress.local", session_factory=_async_maker(), http_client=http
    )


@pytest.mark.asyncio
async def test_worker_sink_delivers_task_ready_to_assigned_worker(engine: Engine):
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="router")
        task = _seed_task(conn, plan)
        _seed_execution(conn, task, "router-worker-x")
    http = _StubHttp()
    await _sink(http).handle(_task_ready_record(task))
    assert len(http.posts) == 1
    _, body = http.posts[0]
    assert body["worker_label"] == "router-worker-x"
    assert body["event_type"] == "task.ready"
    assert body["payload"]["task_id"] == str(task)


@pytest.mark.asyncio
async def test_worker_sink_skips_legacy_substrate(engine: Engine):
    # SC6: a legacy plan is delivered by its agent coordinator (FabricEventSink), never the
    # router's worker sink — else both would deliver.
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="legacy")
        task = _seed_task(conn, plan)
        _seed_execution(conn, task, "w-legacy")
    http = _StubHttp()
    await _sink(http).handle(_task_ready_record(task))
    assert http.posts == []


@pytest.mark.asyncio
async def test_worker_sink_ignores_non_task_ready(engine: Engine):
    http = _StubHttp()
    await _sink(http).handle(
        {"entity_type": "github", "action": "pr_merged",
         "task_id": str(uuid.uuid4()), "payload": {}}
    )
    assert http.posts == []


# ── evaluator verdict loop: rework bumps generation, approve records (alan) ────


def _seed_task_pr(conn, task_id, head) -> None:
    """An OPEN task_prs row so the consumer can back-fill head_sha when a verdict omits it."""
    conn.execute(
        sa.text(
            "INSERT INTO task_prs (repo, pr_number, task_id, head_sha) "
            "VALUES (:r, :n, :t, :h)"
        ),
        {"r": REPO, "n": int(uuid.uuid4().int % 1_000_000), "t": task_id, "h": head},
    )


def _verdict_record(task_id, plan_id, *, verdict, head=None, remediation=None) -> dict:
    payload: dict = {"verdict": verdict}
    if head is not None:
        payload["head_sha"] = head
    if remediation is not None:
        payload["remediation"] = remediation
    return {
        "entity_type": "task",
        "action": "evaluator_verdict",
        "task_id": str(task_id),
        "plan_id": str(plan_id),
        "payload": payload,
    }


def _task_generation(conn, task_id) -> int:
    return conn.execute(
        sa.text("SELECT generation FROM tasks WHERE id=:t"), {"t": task_id}
    ).scalar_one()


def _verdict_markers(conn, task_id) -> int:
    return conn.execute(
        sa.text(
            "SELECT count(*) FROM events "
            "WHERE entity_type='task' AND action='verdict_applied' AND task_id=:t"
        ),
        {"t": task_id},
    ).scalar_one()


def _execs_by_trigger(conn, task_id, trigger) -> int:
    return conn.execute(
        sa.text("SELECT count(*) FROM task_executions WHERE task_id=:t AND trigger=:tr"),
        {"t": task_id, "tr": trigger},
    ).scalar_one()


@pytest.mark.asyncio
async def test_rework_verdict_bumps_generation_and_redispatches_once(engine: Engine):
    """A rework verdict bumps tasks.generation and re-dispatches the author at the new
    generation with the evaluator-rework trigger — exactly once. A re-delivered verdict finds
    the (task, head) marker and no-ops: no second bump, no second author row."""
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="router")
        task = _seed_task(conn, plan)
        _seed_execution(conn, task, "router-worker-x", trigger="initial", generation=1)

    consumer = _consumer()
    rec = _verdict_record(task, plan, verdict="rework", head="H1", remediation="fix the guard")
    await consumer.handle(rec)
    await consumer.handle(rec)  # re-delivery

    with engine.begin() as conn:
        assert _task_generation(conn, task) == 2  # bumped once, not twice
        assert _execs_by_trigger(conn, task, "evaluator-rework") == 1  # one re-dispatch
        assert _execs_by_trigger(conn, task, "initial") == 1  # the original stays
        assert _verdict_markers(conn, task) == 1  # idempotency marker written once
        # the new author execution is at the bumped generation
        assert conn.execute(
            sa.text(
                "SELECT generation FROM task_executions "
                "WHERE task_id=:t AND trigger='evaluator-rework'"
            ),
            {"t": task},
        ).scalar_one() == 2


@pytest.mark.asyncio
async def test_approve_verdict_records_marker_without_dispatch(engine: Engine):
    """An approve verdict records the approval marker and does NOT bump the generation or
    re-dispatch the author (integration is a follow-on). Idempotent on re-delivery."""
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="router")
        task = _seed_task(conn, plan)
        _seed_execution(conn, task, "router-worker-x", trigger="initial", generation=1)

    consumer = _consumer()
    rec = _verdict_record(task, plan, verdict="approve", head="H1")
    await consumer.handle(rec)
    await consumer.handle(rec)  # re-delivery

    with engine.begin() as conn:
        assert _task_generation(conn, task) == 1  # NOT bumped
        assert _execs_by_trigger(conn, task, "evaluator-rework") == 0  # no re-dispatch
        assert _verdict_markers(conn, task) == 1  # one approval marker, despite two deliveries
        assert conn.execute(
            sa.text(
                "SELECT payload->>'decision' FROM events "
                "WHERE entity_type='task' AND action='verdict_applied' AND task_id=:t"
            ),
            {"t": task},
        ).scalar_one() == "approve"


@pytest.mark.asyncio
async def test_verdict_head_sha_falls_back_to_open_pr(engine: Engine):
    """When the verdict omits head_sha, the router back-fills from the task's newest OPEN PR —
    so the idempotency marker still keys correctly and a re-delivery no-ops."""
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="router")
        task = _seed_task(conn, plan)
        _seed_execution(conn, task, "router-worker-x", trigger="initial", generation=1)
        _seed_task_pr(conn, task, "OPENHEAD")  # open PR carries the head

    consumer = _consumer()
    rec = _verdict_record(task, plan, verdict="rework", remediation="do X")  # no head_sha
    await consumer.handle(rec)
    await consumer.handle(rec)

    with engine.begin() as conn:
        assert _task_generation(conn, task) == 2  # applied once via the PR-head fallback
        assert _verdict_markers(conn, task) == 1
        assert conn.execute(
            sa.text(
                "SELECT commit_sha FROM events "
                "WHERE entity_type='task' AND action='verdict_applied' AND task_id=:t"
            ),
            {"t": task},
        ).scalar_one() == "OPENHEAD"


@pytest.mark.asyncio
async def test_verdict_legacy_substrate_ignored(engine: Engine):
    """SC6: a verdict on a legacy-substrate plan is the agent coordinator's business — the
    router does nothing (no bump, no marker)."""
    with engine.begin() as conn:
        _truncate(conn)
        plan = _seed_plan(conn, substrate="legacy")
        task = _seed_task(conn, plan)
        _seed_execution(conn, task, "w-legacy", trigger="initial", generation=1)

    await _consumer().handle(_verdict_record(task, plan, verdict="rework", head="H1"))
    with engine.begin() as conn:
        assert _task_generation(conn, task) == 1
        assert _verdict_markers(conn, task) == 0
