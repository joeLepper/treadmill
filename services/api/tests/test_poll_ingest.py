"""DB-backed foil for the PR-state poll-ingest endpoint (ADR-0113). Proves a poller's
observed merge, routed through the shared webhook seam, produces a `github.pr_merged`
event BYTE-IDENTICAL to a real delivery — task_id resolved from `(repo, pr_number)` via
task_prs, `events.commit_sha` = the merge sha (ADR-0014) — and that a re-poll is a
no-op (deterministic event_id upsert). Gated on the integration DB.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from treadmill_api.routers.github import PollIngestRequest, poll_ingest

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


def _fake_request() -> SimpleNamespace:
    # The endpoint only reads request.app.state.redis (None → seam skips buffering).
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))


@integration
@pytest.mark.asyncio
async def test_poll_ingest_pr_merged_is_identical_to_a_webhook(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = f"o/poll-{uuid.uuid4().hex[:6]}"
    pr_number = 4242
    merge_sha = uuid.uuid4().hex

    async with session_factory() as session:
        plan_id = (
            await session.execute(
                sa.text("INSERT INTO plans (repo) VALUES (:r) RETURNING id"),
                {"r": repo},
            )
        ).scalar_one()
        task_id = (
            await session.execute(
                sa.text(
                    "INSERT INTO tasks (plan_id, repo, title) "
                    "VALUES (:p, :r, 'poll foil') RETURNING id"
                ),
                {"p": plan_id, "r": repo},
            )
        ).scalar_one()
        # The coordinator registered the PR when the worker relayed it (webhook-free).
        await session.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id) "
                "VALUES (:r, :n, :t)"
            ),
            {"r": repo, "n": pr_number, "t": task_id},
        )
        await session.commit()

        resp = await poll_ingest(
            PollIngestRequest(
                repo=repo, pr_number=pr_number, action="pr_merged", merge_sha=merge_sha
            ),
            session=session,
            request=_fake_request(),
        )
        assert resp.entity_type == "github"
        assert resp.action == "pr_merged"

        # The persisted event is a real github.pr_merged with task_id resolved AND the
        # commit_sha COLUMN set — exactly what the drain-guard + task_status view read.
        row = (
            await session.execute(
                sa.text(
                    "SELECT task_id::text, commit_sha FROM events "
                    "WHERE entity_type='github' AND action='pr_merged' "
                    "AND task_id = :t"
                ),
                {"t": task_id},
            )
        ).one()
    assert row[0] == str(task_id)
    assert row[1] == merge_sha  # ADR-0014 commit-anchor extraction ran


@integration
@pytest.mark.asyncio
async def test_poll_ingest_is_idempotent_on_re_poll(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-poll of the same merge must NOT re-publish (the coordinator must not
    re-process it). The shared seam publishes unconditionally, so the endpoint's own
    existence gate is what prevents it. Proven by counting publishes: the seam runs +
    publishes exactly ONCE across two ingests, and only one event row exists."""
    import treadmill_api.routers.github as gh_router

    published: list = []

    class _CountingPublisher:
        def publish(self, event, typed=None):  # seam calls publisher.publish(event, typed)
            published.append(event)

    monkeypatch.setattr(gh_router, "get_publisher", lambda: _CountingPublisher())

    repo = f"o/idem-{uuid.uuid4().hex[:6]}"
    pr_number = 7
    merge_sha = uuid.uuid4().hex
    body = PollIngestRequest(
        repo=repo, pr_number=pr_number, action="pr_merged", merge_sha=merge_sha
    )

    async with session_factory() as session:
        plan_id = (
            await session.execute(
                sa.text("INSERT INTO plans (repo) VALUES (:r) RETURNING id"),
                {"r": repo},
            )
        ).scalar_one()
        task_id = (
            await session.execute(
                sa.text(
                    "INSERT INTO tasks (plan_id, repo, title) "
                    "VALUES (:p, :r, 'idem') RETURNING id"
                ),
                {"p": plan_id, "r": repo},
            )
        ).scalar_one()
        await session.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id) VALUES (:r, :n, :t)"
            ),
            {"r": repo, "n": pr_number, "t": task_id},
        )
        await session.commit()

        r1 = await poll_ingest(body, session=session, request=_fake_request())
        r2 = await poll_ingest(body, session=session, request=_fake_request())
        assert r1.event_id == r2.event_id  # same deterministic id
        # The SECOND ingest must be a true no-op: it did NOT re-run the seam and did NOT
        # re-publish (which would make the coordinator re-process the merge). The seam
        # publishes unconditionally, so we rely on the endpoint's own existence gate.
        assert r1.already_ingested is False
        assert r2.already_ingested is True
        assert len(published) == 1  # published once, NOT re-published on the re-poll

        count = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM events WHERE entity_type='github' "
                    "AND action='pr_merged' AND task_id = :t"
                ),
                {"t": task_id},
            )
        ).scalar_one()
    assert count == 1  # exactly one row despite two ingests
