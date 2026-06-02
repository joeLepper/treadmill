"""Integration test for the webhook-inbox poller (Phase C.1, ADR-0017).

Drives the poller against an in-process ``ThreadedMotoServer`` so we can
exercise the full live-SQS + live-Secrets-Manager + live-Postgres chain
in one shot. The full chain in production is::

    GitHub → API Gateway → Lambda webhook receiver → SQS webhook-inbox
                                                       │
                                          (this poller, locally) ─┘
                                                       │
                                  Postgres events row + SNS publish

This test stands up moto for SQS + Secrets Manager, leaves SNS as a
recording stub (the publish side is exercised by
``test_integration_eventbus_and_pending``), and asserts the row + the
publish both land.

Skipped by default; opt in with ``TREADMILL_INTEGRATION=1``. Requires
``treadmill-local up`` for the live Postgres + ``moto[server]`` in the
api package's dev deps.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
import pytest_asyncio
import redis.asyncio as redis_async
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.coordination.webhook_inbox import WebhookInboxPoller
from treadmill_api.eventbus import LoggingEventPublisher
from treadmill_api.webhooks.pending_events import (
    drain_pending_events,
    pr_pending_buffer_key,
)

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)
DEFAULT_REDIS_URL = "redis://localhost:16379/0"


# ── Fixtures (DB + migrations, mirrored from test_integration_consumer.py) ───


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


# ── Fixtures (Redis — used by the ADR-0063 buffer/drain test) ────────────────


@pytest.fixture
def redis_url() -> str:
    return os.environ.get("TREADMILL_TEST_REDIS_URL", DEFAULT_REDIS_URL)


@pytest_asyncio.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Any]:
    """Real async Redis client; wipes the test's pr:* keys before+after."""
    r = redis_async.Redis.from_url(redis_url, decode_responses=False)
    try:
        keys = await r.keys("pr:*")
        if keys:
            await r.delete(*keys)
        yield r
        keys = await r.keys("pr:*")
        if keys:
            await r.delete(*keys)
    finally:
        await r.aclose()


# ── Fixtures (moto SQS + Secrets Manager) ────────────────────────────────────


@pytest.fixture
def moto_server() -> Iterator[str]:
    """Spin up a fresh ThreadedMotoServer for the duration of one test.

    Mirrors the pattern from ``tools/local-adapter/tests/conftest.py``.
    """
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0)  # ephemeral port
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def boto_kwargs(moto_server: str) -> dict:
    return dict(
        endpoint_url=moto_server,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture
def webhook_inbox_queue_url(boto_kwargs: dict) -> str:
    sqs = boto3.client("sqs", **boto_kwargs)
    resp = sqs.create_queue(QueueName="treadmill-test-webhook-inbox")
    return resp["QueueUrl"]


@pytest.fixture
def webhook_secret_name() -> str:
    return "treadmill-test/github-webhook-secret"


WEBHOOK_SECRET = "an-extremely-secret-shared-key"


@pytest.fixture
def provisioned_secret(
    boto_kwargs: dict, webhook_secret_name: str,
) -> str:
    secrets = boto3.client("secretsmanager", **boto_kwargs)
    secrets.create_secret(
        Name=webhook_secret_name, SecretString=WEBHOOK_SECRET,
    )
    return WEBHOOK_SECRET


# ── Recording publisher ──────────────────────────────────────────────────────


class _RecordingPublisher:
    """In-memory publisher; records every publish so the test can assert
    the poller invoked publish on success."""

    def __init__(self) -> None:
        self.published: list[tuple[object, object]] = []

    async def publish(self, event: object, payload: object) -> None:
        self.published.append((event, payload))


# ── Envelope helpers ─────────────────────────────────────────────────────────


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()


PR_OPENED_RAW = {
    "action": "opened",
    "pull_request": {
        "number": 314,
        "title": "feat: integration",
        "head": {"ref": "task/integration", "sha": "deadbeef" * 5},
        "merged": False,
    },
    "repository": {"full_name": "joe/treadmill"},
    "sender": {"login": "joe"},
}


def _enqueue_envelope(
    *,
    sqs_client,
    queue_url: str,
    body_dict: dict,
    delivery: str,
    secret: str,
    github_event: str = "pull_request",
) -> None:
    body_bytes = json.dumps(body_dict).encode("utf-8")
    envelope = {
        "headers": {
            "x-github-event": github_event,
            "x-github-delivery": delivery,
            "x-hub-signature-256": _sign(body_bytes, secret),
        },
        "body": body_bytes.decode("utf-8"),
    }
    sqs_client.send_message(QueueUrl=queue_url, MessageBody=json.dumps(envelope))


async def _drain_one_message(poller: WebhookInboxPoller) -> None:
    """Drive the poller's poll loop for exactly one receive cycle.

    Avoids running ``poller.start()`` so the test owns the lifecycle.
    """
    resp = await asyncio.to_thread(
        poller.sqs.receive_message,
        QueueUrl=poller.queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=1,
    )
    for message in resp.get("Messages", []):
        await poller._process(message)


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_to_end_webhook_inbox_persists_and_publishes(
    boto_kwargs: dict,
    webhook_inbox_queue_url: str,
    webhook_secret_name: str,
    provisioned_secret: str,
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """End-to-end: enqueue a real-shape envelope on moto SQS, run the
    poller for one cycle, assert the Event row landed with the
    deterministic event_id derived from x-github-delivery, and that the
    publisher was invoked."""
    sqs = boto3.client("sqs", **boto_kwargs)
    secrets_client = boto3.client("secretsmanager", **boto_kwargs)
    publisher = _RecordingPublisher()

    poller = WebhookInboxPoller(
        sqs_client=sqs,
        queue_url=webhook_inbox_queue_url,
        secrets_manager_client=secrets_client,
        webhook_secret_name=webhook_secret_name,
        sessionmaker=session_factory,
        publisher=publisher,
        wait_time_seconds=1,
    )
    # Manually fetch the secret (start() would do this in production; we
    # skip start() so the test isn't racing the background poll loop).
    poller._webhook_secret = await poller._fetch_webhook_secret()

    delivery = "abcdef12-3456-7890-abcd-ef1234567890"
    expected_event_id = uuid.uuid5(uuid.NAMESPACE_OID, delivery)
    _enqueue_envelope(
        sqs_client=sqs,
        queue_url=webhook_inbox_queue_url,
        body_dict=PR_OPENED_RAW,
        delivery=delivery,
        secret=provisioned_secret,
    )

    await _drain_one_message(poller)

    # Event row landed with the deterministic id.
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT id, entity_type, action, payload, commit_sha "
                "FROM events WHERE id = :id"
            ),
            {"id": expected_event_id},
        ).one()
    assert row.entity_type == "github"
    assert row.action == "pr_opened"
    assert row.payload["pr_number"] == 314
    assert row.payload["repo"] == "joe/treadmill"
    assert row.commit_sha == "deadbeef" * 5

    # Publisher fired exactly once for this envelope.
    assert len(publisher.published) == 1

    # SQS queue is drained.
    remaining = sqs.receive_message(
        QueueUrl=webhook_inbox_queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=0,
    ).get("Messages", [])
    assert remaining == []


@pytest.mark.asyncio
async def test_redelivery_with_same_delivery_collapses_to_single_event_row(
    boto_kwargs: dict,
    webhook_inbox_queue_url: str,
    webhook_secret_name: str,
    provisioned_secret: str,
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Two envelopes with the same x-github-delivery (simulating SQS
    visibility-timeout redelivery) derive the same event_id; the second
    INSERT hits ON CONFLICT DO NOTHING and leaves a single row."""
    sqs = boto3.client("sqs", **boto_kwargs)
    secrets_client = boto3.client("secretsmanager", **boto_kwargs)
    publisher = _RecordingPublisher()
    poller = WebhookInboxPoller(
        sqs_client=sqs,
        queue_url=webhook_inbox_queue_url,
        secrets_manager_client=secrets_client,
        webhook_secret_name=webhook_secret_name,
        sessionmaker=session_factory,
        publisher=publisher,
        wait_time_seconds=1,
    )
    poller._webhook_secret = await poller._fetch_webhook_secret()

    delivery = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    for _ in range(2):
        _enqueue_envelope(
            sqs_client=sqs,
            queue_url=webhook_inbox_queue_url,
            body_dict=PR_OPENED_RAW,
            delivery=delivery,
            secret=provisioned_secret,
        )
    # Drain both messages.
    for _ in range(2):
        await _drain_one_message(poller)

    # Single Event row in the DB.
    with engine.connect() as conn:
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM events")
        ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_signature_failure_drops_message_and_writes_no_row(
    boto_kwargs: dict,
    webhook_inbox_queue_url: str,
    webhook_secret_name: str,
    provisioned_secret: str,
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """An envelope signed with the wrong secret is dropped poison-safe;
    no Event row, no publish."""
    sqs = boto3.client("sqs", **boto_kwargs)
    secrets_client = boto3.client("secretsmanager", **boto_kwargs)
    publisher = _RecordingPublisher()
    poller = WebhookInboxPoller(
        sqs_client=sqs,
        queue_url=webhook_inbox_queue_url,
        secrets_manager_client=secrets_client,
        webhook_secret_name=webhook_secret_name,
        sessionmaker=session_factory,
        publisher=publisher,
        wait_time_seconds=1,
    )
    poller._webhook_secret = await poller._fetch_webhook_secret()

    # Sign with the WRONG secret.
    _enqueue_envelope(
        sqs_client=sqs,
        queue_url=webhook_inbox_queue_url,
        body_dict=PR_OPENED_RAW,
        delivery="00000000-bad-sig0-0000-000000000000",
        secret="WRONG-SECRET",
    )

    await _drain_one_message(poller)

    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM events")).scalar()
    assert count == 0
    assert publisher.published == []

    # Message was deleted (poison-safe).
    remaining = sqs.receive_message(
        QueueUrl=webhook_inbox_queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=0,
    ).get("Messages", [])
    assert remaining == []


# ── ADR-0063 Step 1: SQS-path cache-then-heal buffer + drain ─────────────────


PR_OPENED_FOR_BUFFER = {
    "action": "opened",
    "pull_request": {
        "number": 731,
        "title": "feat: ADR-0063 buffer mirror",
        "head": {"ref": "task/cache-then-heal", "sha": "cafef00d" * 5},
        "merged": False,
    },
    "repository": {"full_name": "Joe/Treadmill-Buffer-Test"},
    "sender": {"login": "joe"},
}


@pytest.mark.asyncio
async def test_sqs_pr_opened_without_task_prs_row_buffers_then_drains(
    boto_kwargs: dict,
    webhook_inbox_queue_url: str,
    webhook_secret_name: str,
    provisioned_secret: str,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
    truncate: None,
    engine: Engine,
    async_database_url: str,
) -> None:
    """ADR-0063 Step 1 — SQS-ingress cache-then-heal mirror.

    Steps:
      1. Enqueue a pr_opened envelope on moto SQS for a PR with no
         matching ``task_prs`` row.
      2. Drive the poller for one cycle. The Event row persists with
         ``task_id = NULL``; the pending-events Redis list for the
         (repo, pr_number) pair has exactly one entry.
      3. Seed a ``task_prs`` row that resolves the same (repo, pr_number)
         pair and call ``drain_pending_events`` directly — this mirrors
         the back-fill site in ``coordination/consumer.py`` (the
         ``_write_task_prs_on_completed`` / ``_try_task_prs_fallback_on_pr_merged``
         pair both end in this drain).
      4. Assert the buffered event_id's ``events.task_id`` is now set to
         the seeded task's id.

    This closes the dual-ingress drift the SQS path was carrying since
    ADR-0049: the HTTP route at ``routers/webhooks.py:223-240`` already
    did this; the SQS path silently persisted with ``task_id=NULL`` and
    never came back to heal.
    """
    sqs = boto3.client("sqs", **boto_kwargs)
    secrets_client = boto3.client("secretsmanager", **boto_kwargs)
    publisher = _RecordingPublisher()

    poller = WebhookInboxPoller(
        sqs_client=sqs,
        queue_url=webhook_inbox_queue_url,
        secrets_manager_client=secrets_client,
        webhook_secret_name=webhook_secret_name,
        sessionmaker=session_factory,
        publisher=publisher,
        wait_time_seconds=1,
        redis_client=redis_client,
    )
    poller._webhook_secret = await poller._fetch_webhook_secret()

    delivery = "abcdef00-0000-0000-0000-000000000063"
    expected_event_id = uuid.uuid5(uuid.NAMESPACE_OID, delivery)
    _enqueue_envelope(
        sqs_client=sqs,
        queue_url=webhook_inbox_queue_url,
        body_dict=PR_OPENED_FOR_BUFFER,
        delivery=delivery,
        secret=provisioned_secret,
    )

    await _drain_one_message(poller)

    repo_lower = "joe/treadmill-buffer-test"
    pr_number = 731

    # Event landed; task_id is NULL because no task_prs row resolves yet.
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT id, task_id FROM events WHERE id = :id"),
            {"id": expected_event_id},
        ).one()
    assert row.task_id is None

    # Buffer key holds exactly one entry — the pending event_id.
    key = pr_pending_buffer_key(repo_lower, pr_number)
    assert (await redis_client.llen(key)) == 1

    # Seed the row that would resolve the lookup. Mirrors the shape the
    # consumer's ``_write_task_prs_on_completed`` site INSERTs.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO workflows (id) VALUES ('wf-adr-0063')"
        ))
        wv = conn.execute(sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES ('wf-adr-0063', 1) RETURNING id"
        )).scalar()
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES (:r) RETURNING id"
        ), {"r": repo_lower}).scalar()
        task_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, :r, 'T', :wv) RETURNING id"
        ), {"p": plan_id, "r": repo_lower, "wv": wv}).scalar()
        conn.execute(sa.text(
            "INSERT INTO task_prs (repo, pr_number, task_id) "
            "VALUES (:r, :n, :t)"
        ), {"r": repo_lower, "n": pr_number, "t": task_id})

    # Call drain_pending_events — same call the consumer's back-fill path
    # makes after writing the task_prs row.
    async_engine = create_async_engine(async_database_url)
    Session = async_sessionmaker(async_engine, expire_on_commit=False)
    try:
        async with Session() as session:
            drained = await drain_pending_events(
                redis_client,
                session,
                LoggingEventPublisher(),
                pr_pending_buffer_key(repo_lower, pr_number),
                task_id,
            )
    finally:
        await async_engine.dispose()

    assert drained == 1

    # The Event row now carries the resolved task_id.
    with engine.connect() as conn:
        healed = conn.execute(
            sa.text("SELECT task_id FROM events WHERE id = :id"),
            {"id": expected_event_id},
        ).one()
    assert healed.task_id == task_id

    # Buffer key is drained.
    assert (await redis_client.llen(key)) == 0
