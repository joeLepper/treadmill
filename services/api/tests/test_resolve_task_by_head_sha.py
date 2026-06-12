"""Unit tests for ``resolve_task_by_head_sha`` (task ec0e534c).

The ADR-0063-deferred ``(repo, head_sha)`` lookup; ADR-0090's
CI-observer is its first consumer. Spec cases: present-sha → task,
unknown-sha → None, two task_prs with the same sha → most-recent.

Stub-session style (repo convention): the stub answers ``execute`` with
canned scalar rows; the most-recent ORDER BY is additionally pinned at
the QUERY level (compiled SQL) because a stub cannot prove ordering —
the real-DB proof is the run-log demonstration in the PR.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from treadmill_api.resolvers import resolve_task_by_head_sha


REPO = "joeLepper/treadmill"
SHA = "a" * 40


class _StubSession:
    def __init__(self, first: Any) -> None:
        self._first = first
        self.statements: list[Any] = []

    async def execute(self, stmt: Any):  # noqa: ANN001
        self.statements.append(stmt)

        class _Result:
            def __init__(self, first: Any) -> None:
                self._first = first

            def scalars(self):
                return self

            def first(self):
                return self._first

        return _Result(self._first)


@pytest.mark.anyio
async def test_present_sha_returns_task() -> None:
    task = SimpleNamespace(id=uuid.uuid4(), repo=REPO)
    session = _StubSession(first=task)

    resolved = await resolve_task_by_head_sha(session, REPO, SHA)

    assert resolved is task
    (stmt,) = session.statements
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "JOIN task_prs" in sql
    assert "task_prs.repo =" in sql
    assert "task_prs.head_sha =" in sql


@pytest.mark.anyio
async def test_unknown_sha_returns_none() -> None:
    session = _StubSession(first=None)
    assert await resolve_task_by_head_sha(session, REPO, "f" * 40) is None


@pytest.mark.anyio
async def test_same_sha_query_orders_most_recent_first() -> None:
    """Two task_prs with the same sha: the contract is most-recent wins.
    A stub cannot observe ordering, so pin it at the query level —
    ``ORDER BY created_at DESC LIMIT 1`` — and prove the behavior against
    a real DB in the PR run log."""
    session = _StubSession(first=SimpleNamespace(id=uuid.uuid4()))

    await resolve_task_by_head_sha(session, REPO, SHA)

    (stmt,) = session.statements
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ORDER BY task_prs.created_at DESC" in sql
    assert "LIMIT" in sql


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
