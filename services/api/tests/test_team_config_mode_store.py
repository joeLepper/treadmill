"""DB-backed foil for TeamConfigStore.upsert lifecycle/merge_target semantics
(ADR-0109 / ADR-0110). A stub cannot prove the load-bearing rule: on INSERT the
omitted mode gets the column server-default; on UPDATE the omitted mode is
PRESERVED (a plain re-`team up` must never silently reset a repo's mode). This
also proves the migration adds the columns with the right defaults. Gated on the
integration DB.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from treadmill_api.team_config_store import TeamConfigStore

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
integration = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason="set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL to run",
)


@pytest.fixture(scope="module")
def database_url() -> str:
    return TEST_DB_URL  # type: ignore[return-value]


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str) -> None:
    if not (INTEGRATION and TEST_DB_URL):
        return
    services_api_dir = Path(__file__).resolve().parent.parent
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


@pytest_asyncio.fixture
async def session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async_engine = create_async_engine(database_url.replace("+psycopg", "+asyncpg"))
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    yield factory
    await async_engine.dispose()


@integration
@pytest.mark.asyncio
async def test_insert_omitted_modes_get_server_defaults(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = TeamConfigStore()
    repo = f"o/def-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        row = await store.upsert(session, repo=repo, coordinator_label="c", worker_labels=[])
        await session.commit()
    assert row.lifecycle == "ephemeral"
    assert row.merge_target == "feature-branch"


@integration
@pytest.mark.asyncio
async def test_insert_explicit_modes_are_applied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = TeamConfigStore()
    repo = f"o/set-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        row = await store.upsert(
            session,
            repo=repo,
            coordinator_label="c",
            worker_labels=[],
            lifecycle="persistent",
            merge_target="main",
        )
        await session.commit()
    assert row.lifecycle == "persistent"
    assert row.merge_target == "main"


@integration
@pytest.mark.asyncio
async def test_update_omitting_modes_preserves_them(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The load-bearing rule: a re-`team up` that omits the modes must keep the
    previously-set values, not reset to the server-defaults."""
    store = TeamConfigStore()
    repo = f"o/pres-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        await store.upsert(
            session,
            repo=repo,
            coordinator_label="c",
            worker_labels=[],
            lifecycle="persistent",
            merge_target="main",
        )
        await session.commit()
        # Re-upsert with modes omitted (a plain re-`team up`, adding a worker).
        row = await store.upsert(session, repo=repo, coordinator_label="c", worker_labels=["w-1"])
        await session.commit()
    assert row.lifecycle == "persistent", row.lifecycle
    assert row.merge_target == "main", row.merge_target
    assert row.worker_labels == ["w-1"]


@integration
@pytest.mark.asyncio
async def test_update_explicit_modes_change_them(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = TeamConfigStore()
    repo = f"o/chg-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        await store.upsert(session, repo=repo, coordinator_label="c", worker_labels=[])
        await session.commit()
        row = await store.upsert(
            session,
            repo=repo,
            coordinator_label="c",
            worker_labels=[],
            lifecycle="manual",
            merge_target="main",
        )
        await session.commit()
    assert row.lifecycle == "manual"
    assert row.merge_target == "main"
