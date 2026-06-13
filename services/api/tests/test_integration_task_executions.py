"""Integration tests for task_executions endpoints against a real Postgres.

Covers two things the unit-stub tests cannot:

1. Reconcile SQL correctness — the `_RECONCILE_COORDINATOR_RESTART_SQL`
   WHERE clause is the entire correctness guarantee of the reconcile
   endpoint; a wrong reason string, dropped AND, or wrong status literal
   would pass unit CI silently.

2. GET ?status= filter correctness — `_StubSession.execute` ignores the
   WHERE clause and returns whatever was preloaded, so the unit test only
   proves the endpoint accepts valid values and 422s on invalid ones, NOT
   that the DB query is actually filtered.

Run requirements (the #331 safety rule — no live-DB default):

  TREADMILL_INTEGRATION=1
  TREADMILL_TEST_DATABASE_URL=postgresql+psycopg://.../<DEDICATED TEST DB>

Seeding note: inserts use raw sa.text() for speed and clarity.  The
events.payload column has a server default of '{}'::jsonb; created_at
defaults to now(); task_executions.started_at defaults to NOW().
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason=(
        "set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL "
        "(a DEDICATED test database — this suite truncates task_executions "
        "and events)"
    ),
)

REPO = "joeLepper/treadmill"
WORKER = "worker-treadmill-1"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
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


@pytest.fixture
def seeded_task(engine: Engine) -> Iterator[uuid.UUID]:
    """One plan + one task; cleans up all touched tables on teardown."""
    task_id, plan_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE plans, tasks, task_prs, task_dependencies, "
                "events, task_executions CASCADE"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO plans (id, repo, intent) VALUES (:p, :r, 'exec-integration')"
            ),
            {"p": plan_id, "r": REPO},
        )
        conn.execute(
            sa.text(
                "INSERT INTO tasks (id, plan_id, repo, title) "
                "VALUES (:t, :p, :r, 'exec integration test')"
            ),
            {"t": task_id, "p": plan_id, "r": REPO},
        )
    yield task_id
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE plans, tasks, task_prs, task_dependencies, "
                "events, task_executions CASCADE"
            )
        )


# ── Async call helpers ────────────────────────────────────────────────────────


def _async_url() -> str:
    return TEST_DB_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://")


async def _reconcile() -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from treadmill_api.routers.task_executions import (
        reconcile_coordinator_restart_executions,
    )

    aengine = create_async_engine(_async_url())
    maker = async_sessionmaker(aengine, expire_on_commit=False)
    async with maker() as session:
        resp = await reconcile_coordinator_restart_executions(session)
    await aengine.dispose()
    return resp.reconciled


async def _list_executions(
    task_id: uuid.UUID,
    *,
    status: str | None = None,
) -> list[dict]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from treadmill_api.routers.task_executions import list_task_executions

    aengine = create_async_engine(_async_url())
    maker = async_sessionmaker(aengine, expire_on_commit=False)
    async with maker() as session:
        rows = await list_task_executions(
            session=session,
            task_id=task_id,
            execution_status=status,
        )
    await aengine.dispose()
    return [{"status": r.status, "failure_reason": r.failure_reason} for r in rows]


def _seed_execution(
    conn: sa.Connection,
    *,
    task_id: uuid.UUID,
    exec_status: str,
    failure_reason: str | None = None,
) -> uuid.UUID:
    exec_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO task_executions "
            "(id, task_id, worker_label, trigger, status, failure_reason) "
            "VALUES (:id, :t, :w, 'initial', :s, :fr)"
        ),
        {
            "id": exec_id,
            "t": task_id,
            "w": WORKER,
            "s": exec_status,
            "fr": failure_reason,
        },
    )
    return exec_id


def _seed_pr_merged_event(conn: sa.Connection, task_id: uuid.UUID) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO events (task_id, entity_type, action) "
            "VALUES (:t, 'github', 'pr_merged')"
        ),
        {"t": task_id},
    )


def _fetch_execution(
    conn: sa.Connection, exec_id: uuid.UUID
) -> dict:
    row = conn.execute(
        sa.text(
            "SELECT status, failure_reason FROM task_executions WHERE id = :id"
        ),
        {"id": exec_id},
    ).fetchone()
    assert row is not None, f"execution {exec_id} not found"
    return {"status": row.status, "failure_reason": row.failure_reason}


# ── Reconcile SQL correctness ─────────────────────────────────────────────────


class TestReconcileCoordinatorRestartIntegration:
    """All four scoping cases verified against real Postgres."""

    def test_case_a_coordinator_restart_on_pr_merged_is_restored(
        self, engine: Engine, seeded_task: uuid.UUID,
    ) -> None:
        """Case (a): failed/coordinator_restart on a pr_merged task → flipped
        to completed with failure_reason NULL."""
        with engine.begin() as conn:
            exec_id = _seed_execution(
                conn,
                task_id=seeded_task,
                exec_status="failed",
                failure_reason="coordinator_restart",
            )
            _seed_pr_merged_event(conn, seeded_task)

        reconciled = asyncio.run(_reconcile())

        assert reconciled == 1
        with engine.connect() as conn:
            row = _fetch_execution(conn, exec_id)
        assert row["status"] == "completed"
        assert row["failure_reason"] is None

    def test_case_b_real_failure_reason_on_pr_merged_is_untouched(
        self, engine: Engine, seeded_task: uuid.UUID,
    ) -> None:
        """Case (b): a row with a REAL failure_reason (not coordinator_restart)
        on a pr_merged task must NOT be touched — this is the legitimately-
        failed-row protection."""
        with engine.begin() as conn:
            exec_id = _seed_execution(
                conn,
                task_id=seeded_task,
                exec_status="failed",
                failure_reason="terminal_step_failure",
            )
            _seed_pr_merged_event(conn, seeded_task)

        reconciled = asyncio.run(_reconcile())

        assert reconciled == 0
        with engine.connect() as conn:
            row = _fetch_execution(conn, exec_id)
        assert row["status"] == "failed"
        assert row["failure_reason"] == "terminal_step_failure"

    def test_case_c_coordinator_restart_on_non_terminal_task_is_untouched(
        self, engine: Engine, seeded_task: uuid.UUID,
    ) -> None:
        """Case (c): failed/coordinator_restart on a NON-terminal task must
        NOT be touched — proves the task_status subquery scopes correctly.

        A task whose last execution is 'failed' has derived_status =
        '{worker}: failed', which is NOT in ('pr_merged', 'done', 'cancelled').
        """
        with engine.begin() as conn:
            exec_id = _seed_execution(
                conn,
                task_id=seeded_task,
                exec_status="failed",
                failure_reason="coordinator_restart",
            )
            # No pr_merged event → derived_status = '{worker}: failed' (non-terminal)

        reconciled = asyncio.run(_reconcile())

        assert reconciled == 0
        with engine.connect() as conn:
            row = _fetch_execution(conn, exec_id)
        assert row["status"] == "failed"
        assert row["failure_reason"] == "coordinator_restart"

    def test_case_d_idempotency_second_call_returns_zero(
        self, engine: Engine, seeded_task: uuid.UUID,
    ) -> None:
        """Case (d): calling reconcile a second time returns reconciled=0 —
        proves idempotency at the SQL level, not just via a stubbed rowcount."""
        with engine.begin() as conn:
            _seed_execution(
                conn,
                task_id=seeded_task,
                exec_status="failed",
                failure_reason="coordinator_restart",
            )
            _seed_pr_merged_event(conn, seeded_task)

        first = asyncio.run(_reconcile())
        second = asyncio.run(_reconcile())

        assert first == 1
        assert second == 0

    def test_over_restore_legitimately_failed_exec_on_merged_task_is_flipped(
        self, engine: Engine, seeded_task: uuid.UUID,
    ) -> None:
        """Over-restore: a row that was LEGITIMATELY marked failed/
        coordinator_restart by a real (non-buggy) restart, on a task that
        later reached pr_merged via a retry execution, WILL also be flipped
        to completed.

        DECISION (2026-06-13, task a5685d64): for a pr_merged task this is
        acceptable — the work was eventually completed and merged; flipping
        the old failed execution to completed is accurate-enough history and
        avoids requiring the reconcile to distinguish "was this the buggy
        sweep or a real restart?" — that information is not stored.  The
        cost is a minor cosmetic inaccuracy on an old execution row of an
        already-merged task.
        """
        with engine.begin() as conn:
            # Execution 1: legitimate coordinator_restart (old, failed)
            exec_old = _seed_execution(
                conn,
                task_id=seeded_task,
                exec_status="failed",
                failure_reason="coordinator_restart",
            )
            # Execution 2: successful retry
            _seed_execution(conn, task_id=seeded_task, exec_status="completed")
            # Task reached pr_merged via the retry
            _seed_pr_merged_event(conn, seeded_task)

        reconciled = asyncio.run(_reconcile())

        # Both the old legitimate failure and the buggy-sweep case are
        # flipped — the over-restore is the accepted trade-off.
        assert reconciled == 1
        with engine.connect() as conn:
            row = _fetch_execution(conn, exec_old)
        assert row["status"] == "completed"
        assert row["failure_reason"] is None


# ── Status filter WHERE clause correctness ────────────────────────────────────


class TestStatusFilterIntegration:
    def test_status_running_excludes_completed_and_failed_rows(
        self, engine: Engine, seeded_task: uuid.UUID,
    ) -> None:
        """?status=running must return ONLY running rows even when completed
        and failed rows exist for the same task_id.

        This proves the WHERE status='running' clause is actually applied by
        the database — the unit-stub test cannot verify this because
        _StubSession.execute ignores the query and returns preloaded rows."""
        with engine.begin() as conn:
            _seed_execution(conn, task_id=seeded_task, exec_status="running")
            _seed_execution(conn, task_id=seeded_task, exec_status="completed")
            _seed_execution(conn, task_id=seeded_task, exec_status="failed")

        rows = asyncio.run(_list_executions(seeded_task, status="running"))

        assert len(rows) == 1, f"expected 1 running row, got {len(rows)}: {rows}"
        assert rows[0]["status"] == "running"

    def test_status_omitted_returns_all_rows(
        self, engine: Engine, seeded_task: uuid.UUID,
    ) -> None:
        """No ?status= param must return all rows — backwards-compatible."""
        with engine.begin() as conn:
            _seed_execution(conn, task_id=seeded_task, exec_status="running")
            _seed_execution(conn, task_id=seeded_task, exec_status="completed")
            _seed_execution(conn, task_id=seeded_task, exec_status="failed")

        rows = asyncio.run(_list_executions(seeded_task, status=None))

        assert len(rows) == 3
