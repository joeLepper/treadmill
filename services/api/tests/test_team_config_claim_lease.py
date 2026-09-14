"""DB-backed race foil for the atomic per-repo standup lease
(ADR-0109/0110 step 4, TeamConfigStore.claim). A pure test cannot prove
concurrency: this fires two claims for one repo against a REAL Postgres and asserts
exactly ONE wins (claimed=True) and both see the SAME row — so two concurrent
plan.submitted for a repo yield ONE team and the loser attaches, never a second
team or a dropped plan. Gated on the integration DB.
"""

from __future__ import annotations

import asyncio
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


async def _claim_in_own_txn(
    factory: async_sessionmaker[AsyncSession],
    repo: str,
    coordinator_label: str,
    worker_labels: list[str],
) -> tuple[str, bool]:
    """One full claim in its own session/transaction: claim → commit → release the
    lease. Returns (persisted coordinator_label, claimed)."""
    store = TeamConfigStore()
    async with factory() as session:
        row, claimed = await store.claim(
            session,
            repo=repo,
            coordinator_label=coordinator_label,
            worker_labels=worker_labels,
        )
        await session.commit()
        return row.coordinator_label, claimed


@integration
@pytest.mark.asyncio
async def test_concurrent_claims_yield_one_winner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two concurrent claims for ONE repo → exactly one claimed=True; both see the
    WINNER's row (same coordinator_label). The loser attaches to the standing team;
    it never creates a second team and its call never fails.

    NOTE (Ernie 4a #2): asyncio.gather does not GUARANTEE the two INSERTs overlap —
    a schedule where A commits before B's INSERT begins passes without exercising
    the lock-blocking path. The invariant (one winner, loser attaches) holds either
    way, so this foil proves correctness; a deterministic barrier that forces both
    INSERTs open before either commits lands with 4b, where the blocking path
    becomes load-bearing in the watcher's plan.submitted handler."""
    repo = f"o/lease-{uuid.uuid4().hex[:8]}"
    a, b = await asyncio.gather(
        _claim_in_own_txn(session_factory, repo, "coordinator-A", ["w-a"]),
        _claim_in_own_txn(session_factory, repo, "coordinator-B", ["w-b"]),
    )
    (label_a, claimed_a), (label_b, claimed_b) = a, b

    # Exactly one winner.
    assert claimed_a != claimed_b, (a, b)
    winner = "coordinator-A" if claimed_a else "coordinator-B"
    # BOTH callers see the winner's row — the loser attached, not a second team.
    assert label_a == winner, (a, b)
    assert label_b == winner, (a, b)

    # And the DB holds exactly one row for the repo, the winner's.
    async with session_factory() as session:
        row = await TeamConfigStore().get_by_repo(session, repo)
    assert row is not None
    assert row.coordinator_label == winner


@integration
@pytest.mark.asyncio
async def test_claim_of_standing_team_attaches_not_mutates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A later claim on a standing team returns claimed=False and does NOT mutate
    the standing team's labels — an attach must never disturb the running team
    (unlike upsert, which would overwrite)."""
    repo = f"o/attach-{uuid.uuid4().hex[:8]}"
    store = TeamConfigStore()
    async with session_factory() as session:
        _, claimed_first = await store.claim(
            session,
            repo=repo,
            coordinator_label="coordinator-first",
            worker_labels=["w1", "w2"],
        )
        await session.commit()
    assert claimed_first is True

    async with session_factory() as session:
        row, claimed_second = await store.claim(
            session,
            repo=repo,
            coordinator_label="coordinator-SECOND",
            worker_labels=["different"],
        )
        await session.commit()
    assert claimed_second is False
    # The standing team is untouched — the attach did not overwrite it.
    assert row.coordinator_label == "coordinator-first"
    assert row.worker_labels == ["w1", "w2"]


@integration
@pytest.mark.asyncio
async def test_concurrent_claims_loser_blocks_until_winner_commits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deterministic barrier (Ernie 4a #2 — the non-vacuous race foil): the loser's
    claim PROVABLY blocks on the winner's row lock until the winner commits, then
    unblocks to claimed=False. This forces the lock-blocking path every run, unlike
    asyncio.gather which may schedule the two claims sequentially."""
    repo = f"o/barrier-{uuid.uuid4().hex[:8]}"
    store = TeamConfigStore()
    sess_a = session_factory()
    sess_b = session_factory()
    try:
        # Winner: claim holds the row lock, NOT yet committed.
        row_a, claimed_a = await store.claim(
            sess_a, repo=repo, coordinator_label="A", worker_labels=["wa"]
        )
        assert claimed_a is True

        # Loser: its claim's INSERT must block on the winner's lock.
        task_b = asyncio.create_task(
            store.claim(sess_b, repo=repo, coordinator_label="B", worker_labels=["wb"])
        )
        # Give B's task time to reach — and block at — the INSERT.
        await asyncio.sleep(0.4)
        assert not task_b.done(), (
            "loser's claim must BLOCK on the winner's uncommitted row lock; "
            "if it completed, the lock-blocking path was not exercised"
        )

        # Release: the winner commits → the loser unblocks and sees the conflict.
        await sess_a.commit()
        row_b, claimed_b = await task_b
        await sess_b.commit()

        assert claimed_b is False, "loser must lose"
        assert row_b.coordinator_label == "A", "loser attaches to the winner's row"
    finally:
        await sess_a.close()
        await sess_b.close()


@integration
@pytest.mark.asyncio
async def test_claim_endpoint_wins_then_attaches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The claim ENDPOINT the watcher calls: first claim → claimed=True + the
    written config; a second claim → claimed=False + the standing config (attach),
    untouched. Exercises the wired POST /team_configs/{repo}/claim path."""
    from treadmill_api.routers.team_configs import (
        TeamConfigClaim,
        claim_team_config,
    )

    repo = f"o/ep-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        first = await claim_team_config(
            repo=repo,
            body=TeamConfigClaim(
                coordinator_label="coordinator-ep",
                worker_labels=["w1"],
                lifecycle="ephemeral",
                merge_target="feature-branch",
            ),
            session=session,
        )
    assert first.claimed is True
    assert first.config.coordinator_label == "coordinator-ep"
    assert first.config.lifecycle == "ephemeral"

    async with session_factory() as session:
        second = await claim_team_config(
            repo=repo,
            body=TeamConfigClaim(
                coordinator_label="coordinator-OTHER", worker_labels=["w2"]
            ),
            session=session,
        )
    assert second.claimed is False
    # Attach: the standing team is returned untouched.
    assert second.config.coordinator_label == "coordinator-ep"
    assert second.config.worker_labels == ["w1"]
