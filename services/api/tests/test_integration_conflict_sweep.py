"""Integration tests for the conflict-detection sweep (Week 3 B.3).

Drives ``sweep_open_prs_for_conflicts`` against a live Postgres with a
faked GitHub HTTP client so we can prove the wiring end-to-end without
network access:

  * list-open-PRs returns a deterministic set of PR numbers;
  * get-PR-detail returns a deterministic ``{head_sha, mergeable}`` per
    PR;
  * the sweep emits ``github.pr_conflict`` events with the correct
    ``head_sha`` + ``is_conflicting=True``;
  * the Event row's ``commit_sha`` column is populated (ADR-0014) so
    ADR-0013's mergeability VIEW joins on it;
  * idempotency: re-calling the sweep does not re-emit;
  * mergeable=null retries exactly once.

Gates
-----

These tests require:

  * ``TREADMILL_INTEGRATION=1`` — live Postgres available;
  * ``TREADMILL_SWEEP_TESTS=1`` — opt-in for network-shape mocking
    tests. Defaulting off keeps the integration suite focused on the
    happy path until B.3 is wired into the consumer loop end-to-end.

Run locally:

    treadmill-local up
    TREADMILL_INTEGRATION=1 TREADMILL_SWEEP_TESTS=1 \\
        uv run pytest tests/test_integration_conflict_sweep.py
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.coordination import conflict_sweep as cs_mod
from treadmill_api.coordination.conflict_sweep import sweep_open_prs_for_conflicts

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
SWEEP_TESTS = os.environ.get("TREADMILL_SWEEP_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and SWEEP_TESTS),
    reason=(
        "set TREADMILL_INTEGRATION=1 and TREADMILL_SWEEP_TESTS=1 to run; "
        "requires `treadmill-local up`"
    ),
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
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


_TEST_TABLES = (
    "events",
    "workflow_run_steps",
    "workflow_runs",
    "task_prs",
    "task_dependencies",
    "tasks",
    "plans",
    "workflow_version_steps",
    "workflow_versions",
    "workflows",
    "role_skills",
    "role_hooks",
    "skills",
    "hooks",
    "roles",
    "event_triggers",
)


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    def _do() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(_TEST_TABLES)
                    + " RESTART IDENTITY CASCADE"
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


@pytest.fixture(autouse=True)
def fast_null_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bunkhouse-cribbed null-mergeable retry sleeps 5s by default.
    Shrink to near-zero so the retry test (and any incidental retries)
    doesn't stretch the suite runtime."""
    monkeypatch.setattr(cs_mod, "_NULL_MERGEABLE_RETRY_DELAY_SECONDS", 0.01)


# ── Test helpers ─────────────────────────────────────────────────────────────


def _seed_task_pr(
    engine: Engine,
    *,
    repo: str,
    pr_number: int,
    branch: str = "task/sweep-fix",
) -> uuid.UUID:
    """Insert the minimum graph for a TaskPR row (so ``_resolve_task_id``
    finds a task_id when the sweep emits) and return the task_id."""
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id) VALUES ('wf-author') ON CONFLICT DO NOTHING"
        ))
        wv_id = conn.execute(sa.text(
            "SELECT id FROM workflow_versions "
            "WHERE workflow_id = 'wf-author' AND version = 1"
        )).scalar()
        if wv_id is None:
            wv_id = conn.execute(sa.text(
                "INSERT INTO workflow_versions (workflow_id, version) "
                "VALUES ('wf-author', 1) RETURNING id"
            )).scalar()
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES (:repo) RETURNING id"
        ), {"repo": repo}).scalar()
        task_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, :repo, 't', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv_id, "repo": repo}).scalar()
        conn.execute(sa.text(
            "INSERT INTO task_prs (repo, pr_number, task_id, branch) "
            "VALUES (:r, :n, :t, :b)"
        ), {"r": repo, "n": pr_number, "t": task_id, "b": branch})
    return task_id


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` — only the bits the sweep
    actually reads. Returning these from the fake client keeps the test
    surface tiny."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class _FakeGithubClient:
    """Stand-in for ``httpx.AsyncClient`` — records GET calls and serves
    responses from a per-URL queue. Tests pre-load the queue with the
    responses the sweep should observe; later assertions cover the
    actual URLs hit."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        # url -> list of responses (popped in order). If the queue is
        # empty we'll raise so a missing mock surfaces loudly.
        self.responses: dict[str, list[_FakeResponse]] = {}

    def queue(self, url: str, response: _FakeResponse) -> None:
        self.responses.setdefault(url, []).append(response)

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.calls.append((url, params))
        queue = self.responses.get(url, [])
        if not queue:
            raise AssertionError(
                f"_FakeGithubClient: no response queued for {url} "
                f"(have: {list(self.responses.keys())})"
            )
        return queue.pop(0)


def _list_prs_response(*pr_numbers: int) -> _FakeResponse:
    return _FakeResponse(
        200,
        [{"number": n, "head": {"ref": f"task/branch-{n}"}} for n in pr_numbers],
    )


def _get_pr_response(head_sha: str, mergeable: bool | None) -> _FakeResponse:
    return _FakeResponse(
        200,
        {
            "state": "open",
            "head": {"sha": head_sha},
            "mergeable": mergeable,
        },
    )


class _RecordingPublisher:
    """Captures every published event for assertion."""

    def __init__(self) -> None:
        self.published: list[tuple[Any, Any]] = []

    async def publish(self, event: Any, payload: Any) -> None:
        self.published.append((event, payload))


# ── Test cases ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_emits_conflict_event_for_conflicting_pr(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Mock GitHub returns ``mergeable=false`` for one PR; the sweep
    emits exactly one ``github.pr_conflict`` event with the correct
    ``head_sha`` + ``is_conflicting=True``."""
    repo = "sweep-test/conflicting"
    task_id = _seed_task_pr(engine, repo=repo, pr_number=101)

    head_sha = "deadbeef" * 5
    gh = _FakeGithubClient()
    gh.queue("/repos/sweep-test/conflicting/pulls", _list_prs_response(101))
    gh.queue(
        "/repos/sweep-test/conflicting/pulls/101",
        _get_pr_response(head_sha, mergeable=False),
    )

    publisher = _RecordingPublisher()
    async with session_factory() as session:
        emitted = await sweep_open_prs_for_conflicts(
            session=session,
            publisher=publisher,
            github_client=gh,
            repo=repo,
        )
        await session.commit()

    assert emitted == 1
    # Event row persisted with commit_sha populated (ADR-0014).
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT entity_type, action, commit_sha, task_id, payload "
            "FROM events WHERE entity_type='github' AND action='pr_conflict'"
        )).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_type == "github"
    assert row.action == "pr_conflict"
    assert row.commit_sha == head_sha
    assert row.task_id == task_id
    assert row.payload["is_conflicting"] is True
    assert row.payload["head_sha"] == head_sha
    assert row.payload["pr_number"] == 101

    # Publisher was called once with the matching typed payload.
    assert len(publisher.published) == 1
    _event, typed = publisher.published[0]
    assert typed.repo == repo
    assert typed.pr_number == 101
    assert typed.head_sha == head_sha
    assert typed.is_conflicting is True


@pytest.mark.asyncio
async def test_sweep_skips_non_conflicting_prs(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Mock GitHub returns ``mergeable=true``; the sweep emits no events
    and the publisher is never called."""
    repo = "sweep-test/clean"
    _seed_task_pr(engine, repo=repo, pr_number=202)

    gh = _FakeGithubClient()
    gh.queue("/repos/sweep-test/clean/pulls", _list_prs_response(202))
    gh.queue(
        "/repos/sweep-test/clean/pulls/202",
        _get_pr_response("c0ffee" * 6 + "ab", mergeable=True),
    )

    publisher = _RecordingPublisher()
    async with session_factory() as session:
        emitted = await sweep_open_prs_for_conflicts(
            session=session,
            publisher=publisher,
            github_client=gh,
            repo=repo,
        )
        await session.commit()

    assert emitted == 0
    assert publisher.published == []
    with engine.connect() as conn:
        count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM events "
            "WHERE entity_type='github' AND action='pr_conflict'"
        )).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_sweep_retries_on_null_mergeable(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """First get-PR call returns ``mergeable=null``; the sweep waits the
    (test-shortened) retry delay and re-issues exactly one more
    request. The second response with ``mergeable=false`` causes a
    single pr_conflict emit — proves the retry path actually resolves."""
    repo = "sweep-test/null-retry"
    _seed_task_pr(engine, repo=repo, pr_number=303)

    head_sha = "feedface" * 5
    gh = _FakeGithubClient()
    gh.queue("/repos/sweep-test/null-retry/pulls", _list_prs_response(303))
    # First call: null (GitHub still computing). Second call: false.
    gh.queue(
        "/repos/sweep-test/null-retry/pulls/303",
        _get_pr_response(head_sha, mergeable=None),
    )
    gh.queue(
        "/repos/sweep-test/null-retry/pulls/303",
        _get_pr_response(head_sha, mergeable=False),
    )

    publisher = _RecordingPublisher()
    async with session_factory() as session:
        emitted = await sweep_open_prs_for_conflicts(
            session=session,
            publisher=publisher,
            github_client=gh,
            repo=repo,
        )
        await session.commit()

    assert emitted == 1
    # Two get-PR calls landed (the initial + one retry).
    get_pr_calls = [
        c for c in gh.calls
        if c[0] == "/repos/sweep-test/null-retry/pulls/303"
    ]
    assert len(get_pr_calls) == 2
    assert len(publisher.published) == 1


@pytest.mark.asyncio
async def test_sweep_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Two sweeps over the same conflicting PR at the same HEAD emit
    only one ``pr_conflict`` event. The second sweep sees the existing
    Event row via the (entity_type, action, commit_sha, pr_number)
    probe and short-circuits before persisting / publishing."""
    repo = "sweep-test/idempotent"
    _seed_task_pr(engine, repo=repo, pr_number=404)

    head_sha = "ba5eba11" * 5
    gh = _FakeGithubClient()
    # Same responses queued twice — sweep called twice.
    for _ in range(2):
        gh.queue(
            "/repos/sweep-test/idempotent/pulls", _list_prs_response(404),
        )
        gh.queue(
            "/repos/sweep-test/idempotent/pulls/404",
            _get_pr_response(head_sha, mergeable=False),
        )

    publisher = _RecordingPublisher()
    async with session_factory() as session:
        first = await sweep_open_prs_for_conflicts(
            session=session,
            publisher=publisher,
            github_client=gh,
            repo=repo,
        )
        await session.commit()
    async with session_factory() as session:
        second = await sweep_open_prs_for_conflicts(
            session=session,
            publisher=publisher,
            github_client=gh,
            repo=repo,
        )
        await session.commit()

    assert first == 1
    assert second == 0
    assert len(publisher.published) == 1
    with engine.connect() as conn:
        count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM events "
            "WHERE entity_type='github' AND action='pr_conflict' "
            "AND commit_sha = :s"
        ), {"s": head_sha}).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_sweep_emits_correct_commit_sha(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Per ADR-0014, the Event row's ``commit_sha`` column must match the
    PR's ``head.sha`` so ADR-0013's mergeability VIEW joins on it. This
    test asserts the column value directly — the JSONB payload is
    redundant evidence; the column is the contract."""
    repo = "sweep-test/sha-column"
    _seed_task_pr(engine, repo=repo, pr_number=505)

    expected_sha = "abcd1234" * 5
    gh = _FakeGithubClient()
    gh.queue("/repos/sweep-test/sha-column/pulls", _list_prs_response(505))
    gh.queue(
        "/repos/sweep-test/sha-column/pulls/505",
        _get_pr_response(expected_sha, mergeable=False),
    )

    publisher = _RecordingPublisher()
    async with session_factory() as session:
        emitted = await sweep_open_prs_for_conflicts(
            session=session,
            publisher=publisher,
            github_client=gh,
            repo=repo,
        )
        await session.commit()

    assert emitted == 1
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT commit_sha FROM events "
            "WHERE entity_type='github' AND action='pr_conflict'"
        )).one()
    assert row.commit_sha == expected_sha


@pytest.mark.asyncio
async def test_sweep_short_circuits_when_github_client_is_none(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """When ``github_client`` is ``None`` (GITHUB_TOKEN unset at boot)
    the sweep returns 0 immediately without touching the publisher or
    the session. Keeps un-credentialed API instances usable."""
    publisher = _RecordingPublisher()
    async with session_factory() as session:
        emitted = await sweep_open_prs_for_conflicts(
            session=session,
            publisher=publisher,
            github_client=None,
            repo="sweep-test/unwired",
        )
    assert emitted == 0
    assert publisher.published == []


@pytest.mark.asyncio
async def test_sweep_handles_mixed_mergeable_states(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """A repo with three open PRs: one mergeable, one conflicting, one
    null-after-retry. Only the conflicting PR emits an event."""
    repo = "sweep-test/mixed"
    _seed_task_pr(engine, repo=repo, pr_number=601, branch="task/a")
    _seed_task_pr(engine, repo=repo, pr_number=602, branch="task/b")
    _seed_task_pr(engine, repo=repo, pr_number=603, branch="task/c")

    conflicting_sha = "11112222" * 5
    gh = _FakeGithubClient()
    gh.queue(
        "/repos/sweep-test/mixed/pulls",
        _list_prs_response(601, 602, 603),
    )
    gh.queue(
        "/repos/sweep-test/mixed/pulls/601",
        _get_pr_response("33334444" * 5, mergeable=True),
    )
    gh.queue(
        "/repos/sweep-test/mixed/pulls/602",
        _get_pr_response(conflicting_sha, mergeable=False),
    )
    # PR 603: null on both calls (initial + retry) — sweep gives up and
    # does not emit.
    gh.queue(
        "/repos/sweep-test/mixed/pulls/603",
        _get_pr_response("55556666" * 5, mergeable=None),
    )
    gh.queue(
        "/repos/sweep-test/mixed/pulls/603",
        _get_pr_response("55556666" * 5, mergeable=None),
    )

    publisher = _RecordingPublisher()
    async with session_factory() as session:
        emitted = await sweep_open_prs_for_conflicts(
            session=session,
            publisher=publisher,
            github_client=gh,
            repo=repo,
        )
        await session.commit()

    assert emitted == 1
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT commit_sha, payload FROM events "
            "WHERE entity_type='github' AND action='pr_conflict' "
            "ORDER BY created_at"
        )).all()
    assert len(rows) == 1
    assert rows[0].commit_sha == conflicting_sha
    assert rows[0].payload["pr_number"] == 602
