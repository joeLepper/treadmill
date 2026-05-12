"""Integration test for the plan-merge-to-main trigger (ADR-0021).

Drives the full pipeline against live Postgres + a faked GitHub client:

  1. Construct a ``CoordinationConsumer`` wired against the real
     sessionmaker, a recording publisher, a real Dispatcher, and a
     fake GitHub client.
  2. Send the consumer a ``github.pr_merged`` record naming a PR that
     touched a ``docs/plans/*.md`` with ``status: active``.
  3. Assert: a Plan row landed with the deterministic ``plan_id``,
     Task rows were spawned, ``plan.registered`` / ``plan.activated``
     events landed in the audit log.
  4. Re-deliver the same event. Assert no second Plan was created
     (idempotency on the deterministic plan_id).

Gates
-----

  * ``TREADMILL_INTEGRATION=1`` — live Postgres available;
  * the ``treadmill-local`` stack does not need to be up (we don't
    hit SQS or SNS here; ``CoordinationConsumer.handle()`` runs the
    projection synchronously against the injected sessionmaker).
"""

from __future__ import annotations

import base64
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.coordination import CoordinationConsumer
from treadmill_api.coordination.plan_doc_trigger import derive_plan_id
from treadmill_api.dispatch import Dispatcher

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires Postgres",
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


# ── Test fixtures ───────────────────────────────────────────────────────────


def _seed_wf_author(engine: Engine) -> None:
    """Register ``wf-author`` + a v1 row + a single step. The merge
    handler resolves the workflow slug from the plan-doc YAML so this
    is required for the dispatch path."""
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
        conn.execute(sa.text(
            "INSERT INTO roles (id, model, system_prompt, output_kind) "
            "VALUES ('role-author', 'claude-opus-4-7', '', 'code') "
            "ON CONFLICT DO NOTHING"
        ))
        conn.execute(sa.text(
            "INSERT INTO workflow_version_steps "
            "(workflow_version_id, step_index, step_name, role_id) "
            "VALUES (:wv, 0, 'author', 'role-author') ON CONFLICT DO NOTHING"
        ), {"wv": wv_id})


_PLAN_DOC = """---
status: active
trigger: integration test
---

# Plan: Integration test plan

## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "First task"
    workflow: wf-author
    intent: First task description
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: "tests pass"
```
"""


class _FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class _FakeGithubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
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
                f"_FakeGithubClient: no response queued for {url}"
            )
        return queue.pop(0)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[Any, Any]] = []

    async def publish(self, event: Any, payload: Any) -> None:
        self.published.append((event, payload))


class _Settings:
    """Minimal settings stand-in: empty allow-list = all repos allowed."""

    def plan_merge_repo_is_allowed(self, repo: str) -> bool:
        return True


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


# ── Test cases ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_merged_event_creates_plan_and_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Full end-to-end: ``github.pr_merged`` → trigger fetches doc →
    Plan + Task rows + plan.registered / plan.activated events landed."""
    _seed_wf_author(engine)

    repo = "joeLepper/treadmill"
    pr_number = 17
    merge_sha = "cafe" * 10
    path = "docs/plans/2026-05-12-integration.md"

    gh = _FakeGithubClient()
    gh.queue(
        f"/repos/{repo}/pulls/{pr_number}/files",
        _FakeResponse(200, [{"filename": path, "status": "added"}]),
    )
    gh.queue(
        f"/repos/{repo}/contents/{path}",
        _FakeResponse(200, {"encoding": "base64", "content": _b64(_PLAN_DOC)}),
    )

    publisher = _RecordingPublisher()
    # The Dispatcher writes events + work-queue claims; for this test
    # the SQS client is not required (the dispatch-task path tolerates
    # ``sqs_client=None`` cleanly when the work-queue URL is unset).
    dispatcher = Dispatcher(
        publisher=publisher,
        sqs_client=None,
        work_queue_url=None,
    )
    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=session_factory,
        publisher=publisher,
        dispatcher=dispatcher,
        github_client=gh,
        settings=_Settings(),
    )

    await consumer.handle({
        "entity_type": "github",
        "action": "pr_merged",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": repo,
            "pr_number": pr_number,
            "sender": "joeLepper",
            "merged_sha": merge_sha,
        },
    })

    expected_plan_id = derive_plan_id(repo, path, merge_sha)

    with engine.connect() as conn:
        plan = conn.execute(sa.text(
            "SELECT id, repo, doc_path, created_by FROM plans WHERE id = :id"
        ), {"id": expected_plan_id}).first()
    assert plan is not None
    assert plan.id == expected_plan_id
    assert plan.repo == repo
    assert plan.doc_path == path
    assert plan.created_by == "joeLepper"

    with engine.connect() as conn:
        tasks = conn.execute(sa.text(
            "SELECT id, title FROM tasks WHERE plan_id = :p ORDER BY title"
        ), {"p": expected_plan_id}).all()
    assert len(tasks) == 1
    assert tasks[0].title == "First task"

    with engine.connect() as conn:
        events = conn.execute(sa.text(
            "SELECT entity_type, action FROM events "
            "WHERE plan_id = :p ORDER BY created_at"
        ), {"p": expected_plan_id}).all()
    actions = [(e.entity_type, e.action) for e in events]
    assert ("plan", "registered") in actions
    assert ("plan", "activated") in actions
    assert ("task", "registered") in actions


@pytest.mark.asyncio
async def test_pr_merged_redelivery_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """SQS redelivery of the same ``pr_merged`` event must not create
    a second Plan. The deterministic plan_id + existence probe guard
    against the double-create."""
    _seed_wf_author(engine)

    repo = "joeLepper/treadmill"
    pr_number = 17
    merge_sha = "feed" * 10
    path = "docs/plans/idempotent.md"

    def _fresh_gh() -> _FakeGithubClient:
        gh = _FakeGithubClient()
        gh.queue(
            f"/repos/{repo}/pulls/{pr_number}/files",
            _FakeResponse(200, [{"filename": path, "status": "added"}]),
        )
        gh.queue(
            f"/repos/{repo}/contents/{path}",
            _FakeResponse(200, {
                "encoding": "base64", "content": _b64(_PLAN_DOC),
            }),
        )
        return gh

    publisher = _RecordingPublisher()
    dispatcher = Dispatcher(
        publisher=publisher, sqs_client=None, work_queue_url=None,
    )

    record = {
        "entity_type": "github",
        "action": "pr_merged",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": repo,
            "pr_number": pr_number,
            "sender": "joeLepper",
            "merged_sha": merge_sha,
        },
    }

    # First delivery — plan + task created.
    consumer1 = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory,
        publisher=publisher, dispatcher=dispatcher,
        github_client=_fresh_gh(), settings=_Settings(),
    )
    await consumer1.handle(record)

    # Second delivery — same event, fresh fake gh client (would still
    # return the same plan doc).
    consumer2 = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory,
        publisher=publisher, dispatcher=dispatcher,
        github_client=_fresh_gh(), settings=_Settings(),
    )
    await consumer2.handle(record)

    expected_plan_id = derive_plan_id(repo, path, merge_sha)
    with engine.connect() as conn:
        plan_count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM plans WHERE id = :id"
        ), {"id": expected_plan_id}).scalar()
        task_count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM tasks WHERE plan_id = :p"
        ), {"p": expected_plan_id}).scalar()
    assert plan_count == 1
    assert task_count == 1


@pytest.mark.asyncio
async def test_pr_merged_drafting_plan_persists_observed_inactive_event(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """A merged plan-doc with ``status: drafting`` lands a
    ``plan_doc.observed_inactive`` row in the events table — no Plan
    is created."""
    repo = "joeLepper/treadmill"
    pr_number = 33
    merge_sha = "abad" * 10
    path = "docs/plans/drafting.md"

    drafting_doc = _PLAN_DOC.replace("status: active", "status: drafting")

    gh = _FakeGithubClient()
    gh.queue(
        f"/repos/{repo}/pulls/{pr_number}/files",
        _FakeResponse(200, [{"filename": path, "status": "added"}]),
    )
    gh.queue(
        f"/repos/{repo}/contents/{path}",
        _FakeResponse(200, {
            "encoding": "base64", "content": _b64(drafting_doc),
        }),
    )

    publisher = _RecordingPublisher()
    dispatcher = Dispatcher(
        publisher=publisher, sqs_client=None, work_queue_url=None,
    )
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory,
        publisher=publisher, dispatcher=dispatcher,
        github_client=gh, settings=_Settings(),
    )

    await consumer.handle({
        "entity_type": "github",
        "action": "pr_merged",
        "event_id": str(uuid.uuid4()),
        "payload": {
            "repo": repo,
            "pr_number": pr_number,
            "sender": "joeLepper",
            "merged_sha": merge_sha,
        },
    })

    with engine.connect() as conn:
        plan_count = conn.execute(sa.text("SELECT COUNT(*) FROM plans")).scalar()
        inactive_events = conn.execute(sa.text(
            "SELECT payload FROM events "
            "WHERE entity_type = 'plan_doc' AND action = 'observed_inactive'"
        )).all()
    assert plan_count == 0
    assert len(inactive_events) == 1
    payload = inactive_events[0].payload
    assert payload["status"] == "drafting"
    assert payload["path"] == path
    assert payload["repo"] == repo
