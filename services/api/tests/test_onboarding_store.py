"""Tests for the ADR-0050 onboarding persistence layer.

A non-DB structural check runs always; the round-trip checks against
real Postgres are gated on ``TREADMILL_INTEGRATION=1`` (matching the
shape used by ``tests/test_integration_cross_step.py``).
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

from treadmill_api.models.onboarding import (
    RepoConfigRow,
    RepoContextDocRow,
    RepoProfileRow,
)
from treadmill_api.onboarding_store import OnboardingStore
from treadmill_api.repo_config import RepoConfig
from treadmill_api.repo_profile import RepoProfile


# ── Non-DB structural test (always runs) ─────────────────────────────────────


def test_onboarding_models_and_store_shape() -> None:
    """Models map to the expected table names and the store exposes the
    six accessor methods promised in the ADR-0050 persistence contract.

    Runs without a database — pure import + attribute check.
    """
    assert RepoConfigRow.__tablename__ == "repo_configs"
    assert RepoProfileRow.__tablename__ == "repo_profiles"
    assert RepoContextDocRow.__tablename__ == "repo_context_docs"

    for method in (
        "upsert_repo_config",
        "get_repo_config",
        "upsert_repo_profile",
        "get_repo_profile",
        "record_context_doc",
        "get_context_doc",
    ):
        assert hasattr(OnboardingStore, method), (
            f"OnboardingStore missing {method!r}"
        )


# ── Integration round-trips ──────────────────────────────────────────────────


INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
integration = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


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


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    def _do() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE repo_configs, repo_profiles, "
                    "repo_context_docs RESTART IDENTITY CASCADE"
                )
            )
    _do()
    yield
    _do()


@pytest_asyncio.fixture
async def session_factory(
    async_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async_engine = create_async_engine(async_database_url)
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    yield factory
    await async_engine.dispose()


@integration
@pytest.mark.asyncio
async def test_repo_config_upsert_and_get_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    config = RepoConfig(
        repo=repo,
        mode="adapt",
        auto_merge_blocked=True,
        test_command="uv run pytest",
        lint_command="uv run ruff check",
    )
    async with session_factory() as session:
        await store.upsert_repo_config(session, config)
        await session.commit()

    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched == config

    # Second upsert overwrites the row.
    updated = RepoConfig(
        repo=repo,
        mode="conform",
        auto_merge_blocked=False,
        test_command="make test",
        lint_command=None,
    )
    async with session_factory() as session:
        await store.upsert_repo_config(session, updated)
        await session.commit()

    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched == updated


@integration
@pytest.mark.asyncio
async def test_repo_config_round_trips_claude_account(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """ADR-0055: ``claude_account`` survives upsert/get and an overwrite-to-None."""
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    config = RepoConfig(repo=repo, claude_account="secondary")
    async with session_factory() as session:
        await store.upsert_repo_config(session, config)
        await session.commit()
    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched is not None and fetched.claude_account == "secondary"

    # Clearing it (back to deployment default) is a valid update path.
    cleared = RepoConfig(repo=repo, claude_account=None)
    async with session_factory() as session:
        await store.upsert_repo_config(session, cleared)
        await session.commit()
    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched is not None and fetched.claude_account is None


@integration
@pytest.mark.asyncio
async def test_repo_profile_upsert_and_get_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    profile = RepoProfile(
        repo=repo,
        languages=["python", "typescript"],
        build_command="uv sync",
        test_command="uv run pytest",
        lint_command="uv run ruff check",
        doc_paths=["AGENT.md", "docs/architecture.md", "README.md"],
        components=["services/api", "workers/agent"],
        ci="github-actions",
        has_agent_context=True,
    )
    async with session_factory() as session:
        await store.upsert_repo_profile(session, profile)
        await session.commit()

    async with session_factory() as session:
        fetched = await store.get_repo_profile(session, repo)
    assert fetched is not None
    assert fetched.languages == ["python", "typescript"]
    assert fetched.doc_paths == [
        "AGENT.md",
        "docs/architecture.md",
        "README.md",
    ]
    assert fetched.components == ["services/api", "workers/agent"]
    assert fetched.has_agent_context is True
    assert fetched == profile


@integration
@pytest.mark.asyncio
async def test_record_context_doc_versions_monotonically(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    doc_path = "AGENT.md"
    store = OnboardingStore()

    async with session_factory() as session:
        v1 = await store.record_context_doc(
            session, repo, doc_path, s3_key="ctx/v1", content_sha="a" * 64,
        )
        await session.commit()
    assert v1 == 1

    async with session_factory() as session:
        v2 = await store.record_context_doc(
            session, repo, doc_path, s3_key="ctx/v2", content_sha="b" * 64,
        )
        await session.commit()
    assert v2 == 2

    async with session_factory() as session:
        current = await store.get_context_doc(session, repo, doc_path)
    assert current is not None
    assert current.version == 2
    assert current.s3_key == "ctx/v2"
    assert current.content_sha == "b" * 64
