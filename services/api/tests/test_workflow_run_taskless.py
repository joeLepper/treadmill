"""Taskless workflow runs — schedule-triggered dispatch (ADR-0035).

Schedule-triggered dispatch creates a ``WorkflowRun`` with ``task_id=None``
because schedules sweep a repo, not a specific PR. This module locks the
column nullability in two ways:

1. A non-DB structural assertion against the SQLAlchemy model — always
   runs, catches any future revert of the migration's ORM side.
2. An integration test (gated on ``TREADMILL_INTEGRATION``) that inserts
   a taskless run against the real Postgres via the migration-applied
   ``session_factory`` fixture shape from ``test_integration_cross_step``.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.models.run import WorkflowRun


# ── Structural assertion (always runs) ────────────────────────────────────────


def test_workflow_run_task_id_is_nullable_on_model() -> None:
    """The ORM model declares ``task_id`` as nullable. Locks the migration's
    ORM side so a future revert of ``models/run.py`` re-surfaces here even
    without a Postgres."""
    assert WorkflowRun.__table__.c.task_id.nullable is True


# ── Integration test (real Postgres) ──────────────────────────────────────────

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"


DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def async_database_url(database_url: str) -> str:
    return database_url.replace("+psycopg", "+asyncpg")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = sa.create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str) -> None:
    if not INTEGRATION:
        return
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


@pytest_asyncio.fixture
async def session_factory(
    async_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async_engine = create_async_engine(async_database_url)
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    yield factory
    await async_engine.dispose()


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    tables = (
        "workflow_run_steps",
        "workflow_runs",
        "workflow_version_steps",
        "workflow_versions",
        "workflows",
    )

    def _do() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(tables)
                    + " RESTART IDENTITY CASCADE"
                )
            )
    _do()
    yield
    _do()


@pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)
@pytest.mark.asyncio
async def test_can_insert_workflow_run_with_null_task_id(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """A schedule-triggered run persists with ``task_id=None`` end-to-end.

    Seeds the minimum parents (``workflows`` + ``workflow_versions`` — the
    NOT NULL FK on ``workflow_runs.workflow_version_id`` requires it) via
    raw SQL, then inserts a ``WorkflowRun`` through the ORM with
    ``task_id=None``, commits, reads it back, and asserts the column is
    actually NULL on the row.
    """
    workflow_slug = "wf-scheduled-test"
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id) VALUES (:wf) ON CONFLICT DO NOTHING"
        ), {"wf": workflow_slug})
        wv_id = conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES (:wf, 1) RETURNING id"
        ), {"wf": workflow_slug}).scalar()

    async with session_factory() as session:
        run = WorkflowRun(
            task_id=None,
            workflow_version_id=wv_id,
            trigger="schedule:test",
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    async with session_factory() as session:
        result = await session.execute(
            sa.select(WorkflowRun).where(WorkflowRun.id == run_id)
        )
        fetched = result.scalar_one()
        assert fetched.task_id is None
        assert fetched.workflow_version_id == wv_id
        assert fetched.trigger == "schedule:test"
