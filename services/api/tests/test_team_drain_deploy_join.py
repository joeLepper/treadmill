"""DB-backed foil for the drain-guard's post-merge deploy/staging_smoke join
(ADR-0109 / Ernie BLOCKING 2). A pure test cannot cover this — it runs the real SQL
against real `events` rows across the two DISTINCT streams (deploy: started/succeeded/
failed; staging_smoke: passed/failed, NO 'started'). Gated on the integration DB.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
import subprocess

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.routers.team_configs import _unsettled_deploy_shas

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


async def _emit(session: AsyncSession, entity_type: str, action: str, sha: str) -> None:
    await session.execute(
        sa.text(
            "INSERT INTO events (entity_type, action, commit_sha) "
            "VALUES (:et, :ac, :sha)"
        ),
        {"et": entity_type, "ac": action, "sha": sha},
    )


@integration
@pytest.mark.asyncio
async def test_post_merge_deploy_settling_per_stream(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exact cases from the BLOCKING-2 review — per stream, against real events."""
    import uuid

    p = uuid.uuid4().hex[:8]
    sha_started = f"{p}-started"           # deploy started, no terminal → UNSETTLED
    sha_ok_no_smoke = f"{p}-ok-no-smoke"   # deploy succeeded, no smoke → UNSETTLED (pending)
    sha_full = f"{p}-full"                 # deploy succeeded + smoke passed → settled
    sha_deploy_failed = f"{p}-dfail"       # deploy failed → settled (failure is terminal)
    sha_smoke_failed = f"{p}-sfail"        # deploy succeeded + smoke failed → settled
    sha_none = f"{p}-none"                 # no deploy events (feature-branch) → settled

    async with session_factory() as session:
        await _emit(session, "deploy", "started", sha_started)

        await _emit(session, "deploy", "started", sha_ok_no_smoke)
        await _emit(session, "deploy", "succeeded", sha_ok_no_smoke)

        await _emit(session, "deploy", "started", sha_full)
        await _emit(session, "deploy", "succeeded", sha_full)
        await _emit(session, "staging_smoke", "passed", sha_full)

        await _emit(session, "deploy", "started", sha_deploy_failed)
        await _emit(session, "deploy", "failed", sha_deploy_failed)

        await _emit(session, "deploy", "succeeded", sha_smoke_failed)
        await _emit(session, "staging_smoke", "failed", sha_smoke_failed)
        await session.commit()

        all_shas = [
            sha_started, sha_ok_no_smoke, sha_full, sha_deploy_failed,
            sha_smoke_failed, sha_none,
        ]
        unsettled = await _unsettled_deploy_shas(session, all_shas)

    # Only a deploy still running, or a successful deploy whose smoke has not landed,
    # keeps the coordinator on the hook — the smoke-leg gap the review caught.
    assert unsettled == {sha_started, sha_ok_no_smoke}, unsettled
