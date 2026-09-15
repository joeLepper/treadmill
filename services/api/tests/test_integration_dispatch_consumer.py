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
_TABLES = "plans, tasks, task_prs, task_dependencies, task_executions, events"


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
