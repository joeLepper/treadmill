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
    BinarySpec,
    RepoConfigRow,
    RepoContextDocRow,
    RepoProfileRow,
    RepoWorkerBinaryRow,
    WorkerDeps,
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
    assert RepoWorkerBinaryRow.__tablename__ == "repo_worker_binaries"

    # ADR-0066: fallback column is present on the ORM model.
    assert hasattr(RepoConfigRow, "claude_account_fallback"), (
        "RepoConfigRow missing claude_account_fallback column"
    )

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
            # repo_worker_binaries CASCADEs from repo_configs but the
            # TRUNCATE needs to enumerate it explicitly for RESTART
            # IDENTITY to behave consistently across runs.
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE repo_configs, repo_profiles, "
                    "repo_context_docs, repo_worker_binaries "
                    "RESTART IDENTITY CASCADE"
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
        worker_deps=WorkerDeps(),
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
        worker_deps=WorkerDeps(),
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
async def test_repo_config_round_trips_worker_deps(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """ADR-0059: ``worker_deps`` survives upsert/get (lists + binaries)."""
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    deps = WorkerDeps(
        python=["aws-cdk-lib==2.214.0", "constructs==10.3.0"],
        node=["typescript@5.4.5"],
        binaries=[
            BinarySpec(
                name="cdk",
                download_url="https://example.com/cdk",
                sha256_checksum="a" * 64,
                target_path="/var/treadmill/repo-bin/cdk",
            ),
            BinarySpec(
                name="kubectl",
                download_url="https://example.com/kubectl",
                sha256_checksum="b" * 64,
                target_path="/var/treadmill/repo-bin/kubectl",
            ),
        ],
    )
    config = RepoConfig(repo=repo, worker_deps=deps)
    async with session_factory() as session:
        await store.upsert_repo_config(session, config)
        await session.commit()

    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched is not None
    assert fetched.worker_deps == deps


@integration
@pytest.mark.asyncio
async def test_repo_config_worker_deps_defaults_to_empty_when_omitted(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """ADR-0059: ``get_repo_config`` never returns ``None`` for
    ``worker_deps`` — an empty :class:`WorkerDeps` is the materialized
    shape when the caller upserted without any deps."""
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    config = RepoConfig(repo=repo)  # worker_deps defaults to None
    async with session_factory() as session:
        await store.upsert_repo_config(session, config)
        await session.commit()

    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched is not None
    assert fetched.worker_deps == WorkerDeps()


@integration
@pytest.mark.asyncio
async def test_repo_config_reupsert_replaces_binaries(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """ADR-0059: a re-upsert wipes the previous binaries and inserts
    the new set (drop + insert, not diff)."""
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    first = RepoConfig(
        repo=repo,
        worker_deps=WorkerDeps(
            binaries=[
                BinarySpec(
                    name="cdk",
                    download_url="https://example.com/cdk-old",
                    sha256_checksum="a" * 64,
                    target_path="/var/treadmill/repo-bin/cdk",
                ),
                BinarySpec(
                    name="kubectl",
                    download_url="https://example.com/kubectl",
                    sha256_checksum="b" * 64,
                    target_path="/var/treadmill/repo-bin/kubectl",
                ),
            ]
        ),
    )
    async with session_factory() as session:
        await store.upsert_repo_config(session, first)
        await session.commit()

    second = RepoConfig(
        repo=repo,
        worker_deps=WorkerDeps(
            binaries=[
                BinarySpec(
                    name="terraform",
                    download_url="https://example.com/terraform",
                    sha256_checksum="c" * 64,
                    target_path="/var/treadmill/repo-bin/terraform",
                ),
            ]
        ),
    )
    async with session_factory() as session:
        await store.upsert_repo_config(session, second)
        await session.commit()

    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched is not None
    assert fetched.worker_deps is not None
    assert [b.name for b in fetched.worker_deps.binaries] == ["terraform"]


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


@integration
@pytest.mark.asyncio
async def test_repo_config_round_trips_git_author_override(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """ADR-0076: git author override fields survive upsert/get."""
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    config = RepoConfig(
        repo=repo,
        git_author_name="Joe Lepper",
        git_author_email="josephlepper@gmail.com",
        commit_trailer="",
    )
    async with session_factory() as session:
        await store.upsert_repo_config(session, config)
        await session.commit()

    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched is not None
    assert fetched.git_author_name == "Joe Lepper"
    assert fetched.git_author_email == "josephlepper@gmail.com"
    assert fetched.commit_trailer == ""


@integration
@pytest.mark.asyncio
async def test_repo_config_git_author_override_with_trailer_text(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """ADR-0076: custom trailer text round-trips correctly."""
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    config = RepoConfig(
        repo=repo,
        git_author_name="Jane Doe",
        git_author_email="jane@example.com",
        commit_trailer="Custom-Trailer: value\nAnother: text",
    )
    async with session_factory() as session:
        await store.upsert_repo_config(session, config)
        await session.commit()

    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched is not None
    assert fetched.git_author_name == "Jane Doe"
    assert fetched.git_author_email == "jane@example.com"
    assert fetched.commit_trailer == "Custom-Trailer: value\nAnother: text"


@integration
@pytest.mark.asyncio
async def test_repo_config_git_author_override_check_constraint_rejects_name_without_email(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """ADR-0076: CHECK constraint rejects name without email."""
    import sqlalchemy.exc
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    config = RepoConfig(
        repo=repo,
        git_author_name="Joe Lepper",
        git_author_email=None,
    )
    async with session_factory() as session:
        await store.upsert_repo_config(session, config)
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.commit()


@integration
@pytest.mark.asyncio
async def test_repo_config_git_author_override_defaults_to_none(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """ADR-0076: git author override fields default to None."""
    repo = f"acme/{uuid.uuid4().hex[:8]}"
    store = OnboardingStore()

    config = RepoConfig(repo=repo)
    async with session_factory() as session:
        await store.upsert_repo_config(session, config)
        await session.commit()

    async with session_factory() as session:
        fetched = await store.get_repo_config(session, repo)
    assert fetched is not None
    assert fetched.git_author_name is None
    assert fetched.git_author_email is None
    assert fetched.commit_trailer is None
