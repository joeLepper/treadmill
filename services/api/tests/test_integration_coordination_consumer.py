"""Integration tests for the coordination consumer.

Drives ``consumer.handle()`` against a live Postgres so the JSONB output
column and UUID PKs are exercised exactly as production. The SQS poll
loop itself is exercised via the events SNS → coordination queue path
in the live-API tests in ``test_integration_local.py``.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest tests/test_integration_coordination_consumer.py
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

from treadmill_api.coordination import CoordinationConsumer

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
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


def _seed_step_sync(engine: Engine, *, status: str = "pending") -> uuid.UUID:
    """Insert the minimum graph for a WorkflowRunStep and return its id.

    Synchronous so we can reuse the existing ``engine`` fixture for setup;
    the consumer itself runs against the asyncpg engine via
    ``session_factory``.
    """
    return _seed_full_sync(engine, status=status).step_id


class _SeedHandles:
    def __init__(
        self,
        *,
        step_id: uuid.UUID,
        task_id: uuid.UUID,
        plan_id: uuid.UUID,
        run_id: uuid.UUID,
        repo: str,
    ) -> None:
        self.step_id = step_id
        self.task_id = task_id
        self.plan_id = plan_id
        self.run_id = run_id
        self.repo = repo


def _seed_full_sync(
    engine: Engine,
    *,
    status: str = "pending",
    repo: str = "cli-test/repo",
) -> _SeedHandles:
    """Same as ``_seed_step_sync`` but returns the full id graph so
    tests can assert against task_prs / events tables joined back to
    the originating plan + task."""
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
            "VALUES ('role-author', 'claude-opus-4-7', 'be a coder', 'code') "
            "ON CONFLICT DO NOTHING"
        ))
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES (:repo) RETURNING id"
        ), {"repo": repo}).scalar()
        task_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, :repo, 't', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv_id, "repo": repo}).scalar()
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs (task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'registered') RETURNING id"
        ), {"t": task_id, "wv": wv_id}).scalar()
        step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status) "
            "VALUES (:r, 0, 'author', 'role-author', :s) RETURNING id"
        ), {"r": run_id, "s": status}).scalar()
    return _SeedHandles(
        step_id=step_id, task_id=task_id, plan_id=plan_id,
        run_id=run_id, repo=repo,
    )


# ── handle() — happy paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_started_marks_running(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    step_id = _seed_step_sync(engine)
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "started",
        "step_id": str(step_id),
        "payload": {"started_at": "2026-05-08T10:00:00+00:00"},
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT status, started_at FROM workflow_run_steps WHERE id = :id"
            ),
            {"id": step_id},
        ).one()
    assert row.status == "running"
    assert row.started_at is not None


@pytest.mark.asyncio
async def test_step_completed_marks_completed_and_writes_output(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Per ADR-0012, ``StepCompleted.output`` is the uniform ``StepOutput``
    envelope. The consumer writes the JSON-mode dump to the JSONB column;
    every top-level envelope field round-trips."""
    step_id = _seed_step_sync(engine, status="running")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "branch pushed",
                "decision": "pushed",
                "commit_sha": "deadbeef" * 5,
                "artifacts": [
                    {"kind": "branch", "value": "task/x"},
                    {"kind": "pr_url", "value": "https://x"},
                ],
                "payload": {"pr_number": 42},
            },
        },
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT status, output, completed_at FROM workflow_run_steps "
                "WHERE id = :id"
            ),
            {"id": step_id},
        ).one()
    assert row.status == "completed"
    # Envelope shape lands intact: top-level fields + artifacts + payload.
    assert row.output["summary"] == "branch pushed"
    assert row.output["decision"] == "pushed"
    assert row.output["commit_sha"] == "deadbeef" * 5
    assert row.output["payload"]["pr_number"] == 42
    branches = [
        a["value"] for a in row.output["artifacts"] if a["kind"] == "branch"
    ]
    assert branches == ["task/x"]
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_step_completed_with_token_usage_persists_columns(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """ADR-0020 Wave 1: ``step.completed.token_usage`` lands in the five
    dedicated columns alongside the status flip. The five sub-model
    fields are all required, so a fully-populated event yields a
    fully-populated row."""
    step_id = _seed_step_sync(engine, status="running")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "did the thing",
                "decision": "pushed",
                "artifacts": [],
                "payload": {},
            },
            "token_usage": {
                "input_tokens": 1200,
                "output_tokens": 340,
                "cache_creation_tokens": 50,
                "cache_read_tokens": 800,
                "model": "claude-opus-4-7",
            },
        },
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT status, input_tokens, output_tokens, "
                "cache_creation_tokens, cache_read_tokens, model "
                "FROM workflow_run_steps WHERE id = :id"
            ),
            {"id": step_id},
        ).one()
    assert row.status == "completed"
    assert row.input_tokens == 1200
    assert row.output_tokens == 340
    assert row.cache_creation_tokens == 50
    assert row.cache_read_tokens == 800
    assert row.model == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_step_completed_without_token_usage_leaves_columns_null(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """When ``token_usage`` is absent (validation step, dry-run, taskless
    scheduled tick) the five columns stay NULL — the consumer must NOT
    coerce missing telemetry into zeroes (which would silently corrupt
    any future cost-per-step rollup)."""
    step_id = _seed_step_sync(engine, status="running")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "no llm call",
                "decision": "pass",
                "artifacts": [],
                "payload": {},
            },
        },
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT status, input_tokens, output_tokens, "
                "cache_creation_tokens, cache_read_tokens, model "
                "FROM workflow_run_steps WHERE id = :id"
            ),
            {"id": step_id},
        ).one()
    assert row.status == "completed"
    assert row.input_tokens is None
    assert row.output_tokens is None
    assert row.cache_creation_tokens is None
    assert row.cache_read_tokens is None
    assert row.model is None


@pytest.mark.asyncio
async def test_step_failed_marks_failed_and_records_error(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    step_id = _seed_step_sync(engine, status="running")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "failed",
        "step_id": str(step_id),
        "payload": {
            "failed_at": "2026-05-08T10:30:00+00:00",
            "error": "compilation failed: missing import",
        },
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT status, error FROM workflow_run_steps WHERE id = :id"
            ),
            {"id": step_id},
        ).one()
    assert row.status == "failed"
    assert row.error == "compilation failed: missing import"


@pytest.mark.asyncio
async def test_step_cancelled_only_acts_on_pending(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """``cancelled`` is a no-op once a step has begun running, but works
    on still-pending steps."""
    running_id = _seed_step_sync(engine, status="running")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "cancelled",
        "step_id": str(running_id),
        "payload": {"reason": "task cancelled"},
    })

    pending_id = _seed_step_sync(engine, status="pending")
    await consumer.handle({
        "entity_type": "step",
        "action": "cancelled",
        "step_id": str(pending_id),
        "payload": {"reason": "task cancelled"},
    })

    with engine.connect() as conn:
        rows = {
            r.id: r.status
            for r in conn.execute(
                sa.text("SELECT id, status FROM workflow_run_steps")
            ).all()
        }
    assert rows[running_id] == "running"
    assert rows[pending_id] == "cancelled"


# ── handle() — defensive paths ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_ignores_non_step_events(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """Non-step entity types are no-ops at v0; the consumer logs and moves on."""
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    # Should not raise.
    await consumer.handle({
        "entity_type": "plan",
        "action": "activated",
        "plan_id": str(uuid.uuid4()),
        "payload": {},
    })


@pytest.mark.asyncio
async def test_handle_ignores_unknown_step_action(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    step_id = _seed_step_sync(engine)
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "weird",
        "step_id": str(step_id),
        "payload": {},
    })
    with engine.connect() as conn:
        status = conn.execute(
            sa.text("SELECT status FROM workflow_run_steps WHERE id = :id"),
            {"id": step_id},
        ).scalar()
    assert status == "pending"


@pytest.mark.asyncio
async def test_handle_idempotent_on_completed_step(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Replaying step.completed against an already-completed step is a no-op
    (the WHERE clause filters out completed rows). The second envelope's
    ``summary`` does not overwrite the first."""
    step_id = _seed_step_sync(engine)
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    base = {
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
    }
    await consumer.handle({
        **base,
        "payload": {
            "completed_at": "2026-05-08T10:00:00+00:00",
            "output": {"summary": "first", "decision": "pushed"},
        },
    })
    await consumer.handle({
        **base,
        "payload": {
            "completed_at": "2026-05-08T11:00:00+00:00",
            "output": {"summary": "second", "decision": "pushed"},
        },
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT output FROM workflow_run_steps WHERE id = :id"),
            {"id": step_id},
        ).one()
    assert row.output["summary"] == "first"


# ── Pydantic validation gate (A.3) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_rejects_malformed_started_payload_and_deletes_message(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """A ``step.started`` payload missing the required ``started_at`` field
    fails Pydantic validation. The consumer logs + drops; no DB update."""
    step_id = _seed_step_sync(engine)
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    # No started_at -> ValidationError on StepStarted.model_validate.
    await consumer.handle({
        "entity_type": "step",
        "action": "started",
        "step_id": str(step_id),
        "payload": {},
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT status, started_at FROM workflow_run_steps WHERE id = :id"
            ),
            {"id": step_id},
        ).one()
    assert row.status == "pending"
    assert row.started_at is None


@pytest.mark.asyncio
async def test_handle_drops_malformed_envelope_at_parse_gate(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Per ADR-0012, ``StepCompleted.output`` is the strict ``StepOutput``
    envelope. A malformed envelope (missing required ``summary``/``decision``)
    fails the top-level ``parse_payload`` gate at the entry to ``handle()``
    — same contract as ``test_handle_rejects_malformed_started_payload``.
    The step stays in its prior status; the consumer logs at WARNING and
    drops the message.

    This replaces the Week-2-closure decision-#2 "write raw dict on bad
    author output" path, which was relevant only while the union
    ``AuthorStepOutput | dict`` shape allowed unvalidated dicts to pass
    the top gate. With a uniform envelope, there is no longer a raw-dict
    fallback at the wire boundary."""
    step_id = _seed_step_sync(engine, status="running")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    # Missing required envelope fields — fails StepOutput validation
    # (which is embedded in StepCompleted).
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {"branch": "x", "pr_number": "seven"},
        },
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT status, output FROM workflow_run_steps WHERE id = :id"
            ),
            {"id": step_id},
        ).one()
    # Status untouched — malformed envelope was dropped at the parse gate.
    assert row.status == "running"
    assert row.output is None


# ── Idempotency tightening (A.5) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_idempotent_on_failed_step(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Re-delivering step.failed against an already-failed step leaves the
    error message stable (the WHERE clause filters out failed rows)."""
    step_id = _seed_step_sync(engine, status="running")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    base = {
        "entity_type": "step",
        "action": "failed",
        "step_id": str(step_id),
    }
    await consumer.handle({
        **base,
        "payload": {
            "failed_at": "2026-05-08T10:00:00+00:00",
            "error": "first failure",
        },
    })
    await consumer.handle({
        **base,
        "payload": {
            "failed_at": "2026-05-08T11:00:00+00:00",
            "error": "second failure",
        },
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT status, error FROM workflow_run_steps WHERE id = :id"),
            {"id": step_id},
        ).one()
    assert row.status == "failed"
    assert row.error == "first failure"


@pytest.mark.asyncio
async def test_handle_late_started_after_completed_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """If a step.completed event arrives before step.started (out-of-order
    SNS delivery), the late step.started must not undo the terminal state.
    The completed-step's started_at remains untouched."""
    step_id = _seed_step_sync(engine)
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "payload": {
            "completed_at": "2026-05-08T11:00:00+00:00",
            "output": {
                "summary": "ok",
                "decision": "pushed",
                "artifacts": [{"kind": "branch", "value": "task/x"}],
                "payload": {"pr_number": 1},
            },
        },
    })
    # Late step.started arrives — must be a no-op.
    await consumer.handle({
        "entity_type": "step",
        "action": "started",
        "step_id": str(step_id),
        "payload": {"started_at": "2026-05-08T10:00:00+00:00"},
    })
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT status, started_at FROM workflow_run_steps WHERE id = :id"
            ),
            {"id": step_id},
        ).one()
    assert row.status == "completed"
    # ``started_at`` was never written (we went straight from pending to
    # completed); the late ``started`` event is filtered by the WHERE clause.
    assert row.started_at is None


# ── task_prs writer + pending-events drain (B.8 + D.8) ────────────────────────


@pytest.mark.asyncio
async def test_step_completed_with_pr_writes_task_prs_row(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """``step.completed`` with ``payload.pr_number=42`` writes a task_prs
    row mapping (repo, 42) → task_id. Per ADR-0012's convention map for
    wf-author: ``pr_number`` lives in the envelope's ``payload``;
    ``branch`` comes from the first ``Artifact(kind="branch")``. The
    repo comes from the task's stored row, not the payload (defense
    against payload spoofing)."""
    seed = _seed_full_sync(engine, status="running", repo="b8/test-repo")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(seed.step_id),
        "task_id": str(seed.task_id),
        "run_id": str(seed.run_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "branch pushed",
                "decision": "pushed",
                "commit_sha": "deadbeef" * 5,
                "artifacts": [
                    {"kind": "branch", "value": "task/abc-foo"},
                    {"kind": "pr_url", "value": "https://github.com/x/y/pull/42"},
                ],
                "payload": {"pr_number": 42},
            },
        },
    })

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT repo, pr_number, task_id, branch FROM task_prs"
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.repo == seed.repo
    assert row.pr_number == 42
    assert row.task_id == seed.task_id
    assert row.branch == "task/abc-foo"


@pytest.mark.asyncio
async def test_step_completed_reads_pr_number_from_payload(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Per ADR-0012's convention map for wf-author, ``pr_number`` lives
    in ``output.payload``, not at the envelope's top-level. The consumer
    reads it from there when writing the task_prs row.

    A top-level ``pr_number`` would be rejected by ``StepOutput``'s
    ``extra="forbid"`` — making this the only correct placement."""
    seed = _seed_full_sync(engine, status="running", repo="payload-pr/repo")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(seed.step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "ok",
                "decision": "pushed",
                "artifacts": [{"kind": "branch", "value": "task/payload-pr"}],
                "payload": {"pr_number": 123, "extra_convention_field": "ok"},
            },
        },
    })

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT pr_number, branch FROM task_prs WHERE task_id = :t"
            ),
            {"t": seed.task_id},
        ).one()
    assert row.pr_number == 123


@pytest.mark.asyncio
async def test_step_completed_reads_branch_from_artifact(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Per ADR-0012's convention map for wf-author, ``branch`` lives as
    an ``Artifact(kind="branch", ...)`` in the envelope's ``artifacts``
    list — *not* in ``payload`` (which is for non-typed extras) and not
    at the top level. The consumer reads the first such artifact when
    writing the task_prs row."""
    seed = _seed_full_sync(engine, status="running", repo="artifact-br/repo")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    # Multiple artifacts including a branch — the consumer must pick the
    # ``kind="branch"`` one regardless of ordering.
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(seed.step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "ok",
                "decision": "pushed",
                "artifacts": [
                    {"kind": "pr_url", "value": "https://github.com/x/y/pull/8"},
                    {"kind": "branch", "value": "task/from-artifact"},
                    {"kind": "commit_sha", "value": "abcd"},
                ],
                "payload": {"pr_number": 8},
            },
        },
    })

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT branch FROM task_prs WHERE task_id = :t"),
            {"t": seed.task_id},
        ).one()
    assert row.branch == "task/from-artifact"


@pytest.mark.asyncio
async def test_step_completed_without_pr_skips_task_prs(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Local-mode runs omit ``pr_number`` from the envelope's ``payload``
    (per ADR-0012's convention: absence-by-omission, not a literal
    ``None``). No task_prs row is written for that case — the bridge
    table maps GitHub PRs to tasks and a local commit has no PR to map."""
    seed = _seed_full_sync(engine, status="running", repo="local/run")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(seed.step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "branch pushed (local mode)",
                "decision": "pushed",
                "artifacts": [{"kind": "branch", "value": "task/local-only"}],
                "payload": {},
            },
        },
    })

    with engine.connect() as conn:
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM task_prs")
        ).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_task_prs_insert_is_idempotent_on_redelivery(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Re-delivering ``step.completed`` for the same step (which is a
    no-op on the status UPDATE per the WHERE-clause) must NOT cause a
    duplicate task_prs row — the INSERT is ON CONFLICT DO NOTHING."""
    seed = _seed_full_sync(engine, status="running", repo="idem/repo")
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    event = {
        "entity_type": "step",
        "action": "completed",
        "step_id": str(seed.step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "ok",
                "decision": "pushed",
                "artifacts": [{"kind": "branch", "value": "task/idem"}],
                "payload": {"pr_number": 99},
            },
        },
    }
    await consumer.handle(event)
    await consumer.handle(event)

    with engine.connect() as conn:
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM task_prs WHERE pr_number = 99")
        ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_task_prs_write_uses_task_repo_not_payload_repo(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """The task_prs.repo column is sourced from ``tasks.repo`` via the
    SELECT join — never from the payload. Defense against payload
    spoofing per the 2026-05-11 closure plan B.8.

    Per ADR-0012, the envelope has no ``repo`` field at top-level
    (``extra="forbid"`` would reject one). We instead pin the *positive*
    invariant — when we change ``tasks.repo`` to something a worker
    could not have authored (different casing, suffix), the task_prs
    row carries the task's value.
    """
    seed = _seed_full_sync(
        engine, status="running", repo="Owner/Capitalised-Repo",
    )
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused", sessionmaker=session_factory
    )
    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(seed.step_id),
        "payload": {
            "completed_at": "2026-05-08T10:30:00+00:00",
            "output": {
                "summary": "ok",
                "decision": "pushed",
                "artifacts": [
                    {"kind": "branch", "value": "task/from-task-repo"},
                ],
                "payload": {"pr_number": 7},
            },
        },
    })

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT repo FROM task_prs WHERE pr_number = 7")
        ).one()
    # The task_prs.repo is the task's stored value, verbatim — not a
    # normalized form, not a payload-derived form.
    assert row.repo == "Owner/Capitalised-Repo"


# ── drain_pending_events trigger (D.8) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_task_prs_write_triggers_pending_events_drain(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Pre-buffer a pending event in Redis for (repo, pr_number); drive
    a ``step.completed`` for that PR; assert the buffered event is now
    resolved with the task_id (the consumer called
    ``drain_pending_events``).

    Re-publishing happens through the configured publisher — we plug in
    an in-memory recorder so we can assert the drain re-published.
    """
    import json as _json
    import redis.asyncio as redis_async

    DEFAULT_REDIS_URL = os.environ.get(
        "TREADMILL_TEST_REDIS_URL", "redis://localhost:16379/0",
    )

    seed = _seed_full_sync(engine, status="running", repo="drain/coord")

    # Seed an Event row with task_id=NULL that the drainer will resolve.
    with engine.begin() as conn:
        event_row = conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, payload) "
            "VALUES ('github', 'pr_opened', CAST(:p AS jsonb)) RETURNING id"
        ), {"p": _json.dumps({
            "repo": "drain/coord",
            "pr_number": 77,
            "head_branch": "task/drain",
            "head_sha": "deadbeef" * 5,
            "title": "x",
            "sender": "alice",
        })}).one()
    buffered_event_id = event_row.id

    # Buffer the event into Redis under the (repo, pr_number) key.
    class _Recorder:
        def __init__(self) -> None:
            self.published: list[tuple[Any, Any]] = []

        async def publish(self, event: Any, payload: Any) -> None:
            self.published.append((event, payload))

    recorder = _Recorder()
    redis_client = redis_async.Redis.from_url(
        DEFAULT_REDIS_URL, decode_responses=False,
    )

    # Pre-buffer into Redis matching the pending_events module's key format.
    from treadmill_api.webhooks.pending_events import buffer_pending_event

    try:
        await buffer_pending_event(
            redis_client, "drain/coord", 77, buffered_event_id,
        )

        consumer = CoordinationConsumer(
            sqs_client=None,
            queue_url="unused",
            sessionmaker=session_factory,
            redis_client=redis_client,
            publisher=recorder,
        )
        await consumer.handle({
            "entity_type": "step",
            "action": "completed",
            "step_id": str(seed.step_id),
            "payload": {
                "completed_at": "2026-05-08T10:30:00+00:00",
                "output": {
                    "summary": "ok",
                    "decision": "pushed",
                    "artifacts": [
                        {"kind": "branch", "value": "task/drain"},
                        {"kind": "pr_url", "value": "https://x"},
                    ],
                    "payload": {"pr_number": 77},
                },
            },
        })

        # task_prs row landed.
        with engine.connect() as conn:
            tp = conn.execute(sa.text(
                "SELECT task_id FROM task_prs WHERE repo = :r AND pr_number = 77"
            ), {"r": "drain/coord"}).one()
        assert tp.task_id == seed.task_id

        # The buffered event was resolved — its task_id is now non-NULL.
        with engine.connect() as conn:
            ev = conn.execute(sa.text(
                "SELECT task_id FROM events WHERE id = :id"
            ), {"id": buffered_event_id}).one()
        assert ev.task_id == seed.task_id

        # The drainer re-published the resolved event on the bus.
        assert len(recorder.published) >= 1
        republished_actions = {ev.action for ev, _ in recorder.published}
        assert "pr_opened" in republished_actions
    finally:
        # Clean up the redis key + close the connection.
        try:
            await redis_client.delete("pr:drain/coord:77:pending_events")
        finally:
            await redis_client.aclose()


# ── Cross-step regression (Week-3 B.2) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_step_wf_author_does_not_dispatch_next_step(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """Regression for Week-3 B.2: completing the only step in a
    single-step run (e.g. wf-author) must not dispatch any next step.

    The cross-step dispatch path was added in B.2 to fire ``step.ready``
    for step N+1 after step N completes. wf-author has only step 0, so
    the next-step SELECT returns nothing and the path short-circuits
    silently. No spurious ``step.ready`` events; no SQS claims.

    Constructs the consumer *with* a dispatcher (otherwise cross-step
    is a no-op trivially — that masks the regression we're guarding
    against, where the helper might erroneously emit a step.ready for
    a non-existent step 1).
    """
    from treadmill_api.dispatch import Dispatcher

    seed = _seed_full_sync(engine, status="running", repo="wf-author/regress")

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, Any]] = []

        async def publish(self, event: Any, payload: Any) -> None:
            self.calls.append((event, payload))

    class _RecorderSqs:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        def send_message(self, **kwargs: Any) -> None:
            self.sent.append(kwargs)

    publisher = _Recorder()
    sqs = _RecorderSqs()
    dispatcher = Dispatcher(
        publisher=publisher, sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    consumer = CoordinationConsumer(
        sqs_client=None, queue_url="unused",
        sessionmaker=session_factory, dispatcher=dispatcher,
    )

    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(seed.step_id),
        "task_id": str(seed.task_id),
        "run_id": str(seed.run_id),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-11T10:30:00+00:00",
            "output": {
                "summary": "branch pushed",
                "decision": "pushed",
                "commit_sha": "deadbeef" * 5,
                "artifacts": [
                    {"kind": "branch", "value": "task/regress"},
                ],
                "payload": {"pr_number": 42},
            },
        },
    })

    # No new step.ready event emerged.
    with engine.connect() as conn:
        count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM events "
            "WHERE entity_type = 'step' AND action = 'ready'"
        )).scalar()
    assert count == 0
    # No publish + no SQS send from the cross-step path. (Publisher.calls
    # is empty because the consumer doesn't auto-publish step.completed —
    # it just projects the row.)
    assert publisher.calls == []
    assert sqs.sent == []
