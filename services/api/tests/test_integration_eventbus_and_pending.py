"""Integration tests for the SNS-backed event publisher and the
cache-then-heal pending-events buffering.

Skipped by default; opt in with ``TREADMILL_INTEGRATION=1``.

These tests exercise:
  * end-to-end SNS publish from the live API into moto, verified by
    subscribing a temporary SQS queue to the events topic and asserting
    the published message lands.
  * pending-event buffering when a webhook arrives without a resolved
    task_id, asserted by inspecting Redis directly.
  * the ``drain_pending_events`` utility re-publishes buffered events
    with task_id resolved (called directly from the test to simulate
    the future task_prs INSERT path).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import boto3
import httpx
import pytest
import redis.asyncio as redis_async
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


DEFAULT_API_URL = "http://localhost:8088"
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)
DEFAULT_REDIS_URL = "redis://localhost:16379/0"
DEFAULT_AWS_ENDPOINT = "http://localhost:5001"


@pytest.fixture(scope="module")
def api_url() -> str:
    return os.environ.get("TREADMILL_API_URL", DEFAULT_API_URL)


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def aws_endpoint_url() -> str:
    return os.environ.get("AWS_ENDPOINT_URL", DEFAULT_AWS_ENDPOINT)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Engine:
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


@pytest.fixture(scope="module")
def client(api_url: str) -> Iterator[httpx.Client]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    with httpx.Client(base_url=api_url, timeout=10.0) as c:
        yield c


@pytest.fixture(scope="module")
def boto_kwargs(aws_endpoint_url: str) -> dict:
    return dict(
        endpoint_url=aws_endpoint_url,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


_TEST_TABLES = (
    "plans", "tasks", "task_prs", "task_dependencies",
    "workflow_runs", "workflow_run_steps", "events",
    "event_triggers",
    "workflows", "workflow_versions", "workflow_version_steps",
    "roles", "skills", "hooks",
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


@pytest.fixture
def clear_redis() -> Iterator[None]:
    """Wipe the redis test keys before + after each test."""
    async def _clear():
        r = redis_async.Redis.from_url(DEFAULT_REDIS_URL, decode_responses=False)
        try:
            keys = await r.keys("pr:*")
            if keys:
                await r.delete(*keys)
        finally:
            await r.aclose()
    asyncio.get_event_loop().run_until_complete(_clear())
    yield
    asyncio.get_event_loop().run_until_complete(_clear())


# ── SNS publish — subscribe a temp queue to the events topic ─────────────────


def test_webhook_publishes_to_events_topic(
    client: httpx.Client,
    truncate: None,
    clear_redis: None,
    boto_kwargs: dict,
) -> None:
    """Subscribe a temp SQS queue to ``treadmill-events``, send a webhook,
    pull from SQS, assert the published message attributes and body."""
    sns = boto3.client("sns", **boto_kwargs)
    sqs = boto3.client("sqs", **boto_kwargs)

    # Find the events topic (the local adapter provisioned it from CDK).
    topics = sns.list_topics()["Topics"]
    events_topic_arn = next(
        t["TopicArn"] for t in topics
        if t["TopicArn"].endswith(":treadmill-events")
    )

    # Create a temp queue and subscribe it.
    queue = sqs.create_queue(QueueName=f"test-events-{int(time.time())}")
    queue_url = queue["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    sns.subscribe(
        TopicArn=events_topic_arn,
        Protocol="sqs",
        Endpoint=queue_arn,
        Attributes={"RawMessageDelivery": "true"},
    )

    # Send a webhook (will publish a github.pr_opened event).
    webhook_body = {
        "action": "opened",
        "pull_request": {
            "number": 7, "title": "publish test", "merged": False,
            "head": {"ref": "task/x", "sha": "deadbeef" * 5},
        },
        "repository": {"full_name": "publish/test-repo"},
        "sender": {"login": "alice"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(webhook_body),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202

    # Pull from the temp queue and assert the published message landed.
    deadline = time.monotonic() + 5
    msg = None
    while time.monotonic() < deadline:
        recv = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=1,
        )
        if recv.get("Messages"):
            msg = recv["Messages"][0]
            break
    assert msg is not None, "no message landed on the events queue"
    body = json.loads(msg["Body"])
    assert body["entity_type"] == "github"
    assert body["action"] == "pr_opened"
    assert body["payload"]["pr_number"] == 7
    assert body["payload"]["repo"] == "publish/test-repo"


# ── Pending-events buffering ─────────────────────────────────────────────────


def test_webhook_without_task_id_buffers_event(
    client: httpx.Client,
    truncate: None,
    clear_redis: None,
) -> None:
    """A pr_opened webhook for an unknown PR persists with task_id=NULL
    and lands in the Redis pending-events buffer."""
    body = {
        "action": "opened",
        "pull_request": {
            "number": 99, "title": "pending", "merged": False,
            "head": {"ref": "task/x", "sha": "deadbeef" * 5},
        },
        "repository": {"full_name": "Pending/Repo"},
        "sender": {"login": "alice"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202
    assert response.json()["task_id"] is None

    # Confirm a buffer entry exists. Repo is lowercased per the case-
    # insensitive task_prs lookup convention.
    async def _check():
        r = redis_async.Redis.from_url(DEFAULT_REDIS_URL, decode_responses=False)
        try:
            return await r.llen("pr:pending/repo:99:pending_events")
        finally:
            await r.aclose()
    count = asyncio.get_event_loop().run_until_complete(_check())
    assert count == 1


def test_webhook_with_task_id_does_not_buffer(
    client: httpx.Client,
    truncate: None,
    clear_redis: None,
    engine: Engine,
) -> None:
    """When task_prs already maps the PR, the webhook resolves task_id
    directly and does NOT buffer in Redis."""
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO workflows (id) VALUES ('wf-x')"))
        wv = conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES ('wf-x', 1) RETURNING id"
        )).scalar()
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES ('test/repo') RETURNING id"
        )).scalar()
        task_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, 'test/repo', 'T', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv}).scalar()
        conn.execute(sa.text(
            "INSERT INTO task_prs (repo, pr_number, task_id) "
            "VALUES ('test/repo', 88, :t)"
        ), {"t": task_id})

    body = {
        "action": "opened",
        "pull_request": {
            "number": 88, "title": "x", "merged": False,
            "head": {"ref": "task/x", "sha": "deadbeef" * 5},
        },
        "repository": {"full_name": "test/repo"},
        "sender": {"login": "alice"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202
    assert response.json()["task_id"] == str(task_id)

    async def _check():
        r = redis_async.Redis.from_url(DEFAULT_REDIS_URL, decode_responses=False)
        try:
            return await r.llen("pr:test/repo:88:pending_events")
        finally:
            await r.aclose()
    count = asyncio.get_event_loop().run_until_complete(_check())
    assert count == 0


# ── drain_pending_events utility ─────────────────────────────────────────────


def test_drain_resolves_buffered_events(
    client: httpx.Client,
    truncate: None,
    clear_redis: None,
    engine: Engine,
    database_url: str,
) -> None:
    """1. Send a webhook with no task_prs row → event buffered.
       2. Insert a task_prs row.
       3. Call drain_pending_events.
       4. Assert the event row's task_id is now resolved.
    """
    body = {
        "action": "opened",
        "pull_request": {
            "number": 55, "title": "x", "merged": False,
            "head": {"ref": "task/x", "sha": "deadbeef" * 5},
        },
        "repository": {"full_name": "drain/test"},
        "sender": {"login": "alice"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    assert response.json()["task_id"] is None

    # Seed task_prs row that the buffered event was waiting for.
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO workflows (id) VALUES ('wf-d')"))
        wv = conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES ('wf-d', 1) RETURNING id"
        )).scalar()
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES ('drain/test') RETURNING id"
        )).scalar()
        task_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, 'drain/test', 'T', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv}).scalar()
        conn.execute(sa.text(
            "INSERT INTO task_prs (repo, pr_number, task_id) "
            "VALUES ('drain/test', 55, :t)"
        ), {"t": task_id})

    # Call drain_pending_events directly (the future task_prs INSERT path
    # will invoke this; here we simulate it).
    from treadmill_api.eventbus import LoggingEventPublisher
    from treadmill_api.webhooks.pending_events import (
        drain_pending_events,
        pr_pending_buffer_key,
    )

    async def _drain():
        async_url = database_url.replace("+psycopg", "+asyncpg")
        async_engine = create_async_engine(async_url)
        Session = async_sessionmaker(async_engine, expire_on_commit=False)
        r = redis_async.Redis.from_url(DEFAULT_REDIS_URL, decode_responses=False)
        try:
            async with Session() as session:
                drained = await drain_pending_events(
                    r, session, LoggingEventPublisher(),
                    pr_pending_buffer_key("drain/test", 55), task_id,
                )
            return drained
        finally:
            await r.aclose()
            await async_engine.dispose()

    drained = asyncio.get_event_loop().run_until_complete(_drain())
    assert drained == 1

    # Confirm the event row's task_id is now resolved.
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT task_id FROM events WHERE id = :id"),
            {"id": event_id},
        ).one()
    assert row.task_id == task_id


def test_drain_on_empty_buffer_returns_zero(
    truncate: None, clear_redis: None, database_url: str,
) -> None:
    """An empty buffer drains cleanly with no errors."""
    from treadmill_api.eventbus import LoggingEventPublisher
    from treadmill_api.webhooks.pending_events import (
        drain_pending_events,
        pr_pending_buffer_key,
    )

    async def _drain():
        async_url = database_url.replace("+psycopg", "+asyncpg")
        async_engine = create_async_engine(async_url)
        Session = async_sessionmaker(async_engine, expire_on_commit=False)
        r = redis_async.Redis.from_url(DEFAULT_REDIS_URL, decode_responses=False)
        try:
            async with Session() as session:
                import uuid as _uuid
                drained = await drain_pending_events(
                    r, session, LoggingEventPublisher(),
                    pr_pending_buffer_key("no/such/repo", 1), _uuid.uuid4(),
                )
            return drained
        finally:
            await r.aclose()
            await async_engine.dispose()

    drained = asyncio.get_event_loop().run_until_complete(_drain())
    assert drained == 0
