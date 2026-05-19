"""Integration test: end-to-end supersede flow (ADR-0048).

Drives the full chain:
  1. A parent task is registered with a deliberately-broken description.
  2. wf-author fails with the no-diff error (mocked via CoordinationConsumer).
  3. wf-architecture-resolve is dispatched (verified in DB).
  4. The architect emits verdict=supersede with a rewritten_description.
  5. A child task is created (parent_task_id set, title suffixed, description
     rewritten); a fresh wf-author run is dispatched against the child.
  6. The child's wf-author completes with a real PR artifact (mocked).
  7. task_prs row is written for the child; task_mergeability VIEW shows
     'mergeable' after wf-review (approved) + wf-validate (pass) are seeded.
  8. The auto-merge deadline is set in Redis after a wf-validate step.completed
     is driven through the consumer with a wired Redis mock.

PR #181 covered the trigger-level mechanics (test_supersede_trigger.py). This
test closes the gap for the full flow: trigger + child task creation + fresh
dispatch + worker pickup (mocked) + mergeability + auto-merge.

Pattern after test_integration_task_retry.py — same shape of integration
harness, same SQS+Postgres+API mocking.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest services/api/tests/test_integration_supersede_end_to_end.py
"""

from __future__ import annotations

import json
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
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.coordination import CoordinationConsumer
from treadmill_api.coordination.triggers import AUTO_MERGE_DEADLINE_KEY_PREFIX
from treadmill_api.dispatch import Dispatcher

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)

_TEST_TABLES = (
    "events",
    "workflow_dispatch_dedup",
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
    "schedules",
)

_REPO = "test/supersede-e2e"
_HEAD_SHA = "abc123def456abc123def456abc123def456"
_PR_NUMBER = 77
_BRANCH = "task/abc12345-add-feature-x-superseded"
_REWRITTEN_DESC = (
    "Write services/api/treadmill_api/feature_x.py with function add_x()."
)


# ── Module fixtures ────────────────────────────────────────────────────────────


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


@pytest_asyncio.fixture(scope="module")
async def session_factory(
    async_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async_engine = create_async_engine(async_database_url)
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    yield factory
    await async_engine.dispose()


# ── Function fixtures ──────────────────────────────────────────────────────────


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


# ── Test doubles ───────────────────────────────────────────────────────────────


class _RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    async def publish(self, event: Any, payload: Any) -> None:
        self.calls.append((event, payload))


class _RecordingSqs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


def _mock_redis() -> Any:
    """Minimal async Redis mock that records `set` calls.

    ``exists`` returns 0 so ``maybe_auto_merge_on_mergeable`` does not
    bail out on the "already fired" guard.
    """
    redis = AsyncMock()
    redis.exists.return_value = 0
    redis.set.return_value = True
    return redis


def _make_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    publisher: _RecordingPublisher,
    sqs: _RecordingSqs,
    *,
    redis_client: Any = None,
) -> CoordinationConsumer:
    dispatcher = Dispatcher(
        publisher=publisher,
        sqs_client=sqs,
        work_queue_url="https://sqs.example.com/work",
    )
    return CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=session_factory,
        dispatcher=dispatcher,
        redis_client=redis_client,
    )


# ── Seed helpers ───────────────────────────────────────────────────────────────


def _seed_role(conn: Connection, role_id: str, output_kind: str = "code") -> None:
    conn.execute(
        sa.text(
            "INSERT INTO roles (id, model, system_prompt, output_kind) "
            "VALUES (:id, 'claude', '', :kind) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": role_id, "kind": output_kind},
    )


def _seed_workflow_version(
    conn: Connection,
    slug: str,
    role_id: str,
    step_name: str = "run",
) -> uuid.UUID:
    """Idempotent: register workflow + version + one step, return version id."""
    existing = conn.execute(
        sa.text(
            "SELECT wv.id FROM workflow_versions wv "
            "WHERE wv.workflow_id = :s AND wv.version = 1"
        ),
        {"s": slug},
    ).scalar()
    if existing is not None:
        return existing

    conn.execute(
        sa.text(
            "INSERT INTO workflows (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": slug},
    )
    wv_id: uuid.UUID = conn.execute(
        sa.text(
            "INSERT INTO workflow_versions (workflow_id, version) "
            "VALUES (:s, 1) RETURNING id"
        ),
        {"s": slug},
    ).scalar()
    conn.execute(
        sa.text(
            "INSERT INTO workflow_version_steps "
            "(workflow_version_id, step_index, step_name, role_id) "
            "VALUES (:wv, 0, :name, :role)"
        ),
        {"wv": wv_id, "name": step_name, "role": role_id},
    )
    return wv_id


def _seed_all_workflows(engine: Engine) -> dict[str, uuid.UUID]:
    """Seed roles and workflow versions for all workflows used in the flow."""
    with engine.begin() as conn:
        _seed_role(conn, "role-author")
        _seed_role(conn, "role-architect", "analysis")
        _seed_role(conn, "role-reviewer", "analysis")
        _seed_role(conn, "role-validator", "analysis")
        wf_author = _seed_workflow_version(conn, "wf-author", "role-author")
        wf_arch = _seed_workflow_version(
            conn, "wf-architecture-resolve", "role-architect", "resolve"
        )
        wf_review = _seed_workflow_version(conn, "wf-review", "role-reviewer", "review")
        wf_validate = _seed_workflow_version(
            conn, "wf-validate", "role-validator", "validate"
        )
    return {
        "wf-author": wf_author,
        "wf-architecture-resolve": wf_arch,
        "wf-review": wf_review,
        "wf-validate": wf_validate,
    }


def _make_plan(engine: Engine, repo: str = _REPO) -> uuid.UUID:
    with engine.begin() as conn:
        plan_id: uuid.UUID = conn.execute(
            sa.text("INSERT INTO plans (repo) VALUES (:repo) RETURNING id"),
            {"repo": repo},
        ).scalar()
    return plan_id


def _make_task(
    engine: Engine,
    plan_id: uuid.UUID,
    wv_id: uuid.UUID,
    *,
    repo: str = _REPO,
    title: str = "Add feature X",
    description: str = "Broken description that author cannot implement.",
) -> uuid.UUID:
    with engine.begin() as conn:
        task_id: uuid.UUID = conn.execute(
            sa.text(
                "INSERT INTO tasks (plan_id, repo, title, description, workflow_version_id) "
                "VALUES (:plan_id, :repo, :title, :desc, :wv) RETURNING id"
            ),
            {
                "plan_id": plan_id,
                "repo": repo,
                "title": title,
                "desc": description,
                "wv": wv_id,
            },
        ).scalar()
    return task_id


def _make_run_with_step(
    engine: Engine,
    task_id: uuid.UUID,
    wv_id: uuid.UUID,
    *,
    trigger: str = "registered",
    step_status: str = "pending",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a single-step workflow run and return (run_id, step_id)."""
    with engine.begin() as conn:
        run_id: uuid.UUID = conn.execute(
            sa.text(
                "INSERT INTO workflow_runs (task_id, workflow_version_id, trigger) "
                "VALUES (:t, :wv, :trig) RETURNING id"
            ),
            {"t": task_id, "wv": wv_id, "trig": trigger},
        ).scalar()
        row = conn.execute(
            sa.text(
                "SELECT step_name, role_id FROM workflow_version_steps "
                "WHERE workflow_version_id = :wv AND step_index = 0"
            ),
            {"wv": wv_id},
        ).one()
        step_id: uuid.UUID = conn.execute(
            sa.text(
                "INSERT INTO workflow_run_steps "
                "(run_id, step_index, step_name, role_id, status) "
                "VALUES (:r, 0, :name, :role, :st) RETURNING id"
            ),
            {"r": run_id, "name": row.step_name, "role": row.role_id, "st": step_status},
        ).scalar()
    return run_id, step_id


def _get_wf_runs_for_task(
    engine: Engine,
    task_id: uuid.UUID,
    workflow_slug: str,
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Return [(run_id, step_id), ...] newest run first."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT wr.id AS run_id, wrs.id AS step_id "
                "FROM workflow_runs wr "
                "JOIN workflow_versions wv ON wv.id = wr.workflow_version_id "
                "JOIN workflow_run_steps wrs ON wrs.run_id = wr.id "
                "WHERE wr.task_id = :t AND wv.workflow_id = :slug "
                "ORDER BY wr.created_at DESC"
            ),
            {"t": task_id, "slug": workflow_slug},
        ).all()
    return [(r.run_id, r.step_id) for r in rows]


def _get_child_tasks(engine: Engine, parent_task_id: uuid.UUID) -> list[Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT id, title, description, parent_task_id, created_by "
                "FROM tasks WHERE parent_task_id = :p"
            ),
            {"p": parent_task_id},
        ).all()
    return rows


def _get_task_prs(engine: Engine, task_id: uuid.UUID) -> list[Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT repo, pr_number, branch FROM task_prs WHERE task_id = :t"
            ),
            {"t": task_id},
        ).all()
    return rows


def _get_mergeability(engine: Engine, task_id: uuid.UUID) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT derived_mergeability FROM task_mergeability "
                "WHERE task_id = CAST(:t AS uuid)"
            ),
            {"t": str(task_id)},
        ).first()
    return row.derived_mergeability if row is not None else None


def _dedup_key_exists(engine: Engine, dedup_key: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT 1 FROM workflow_dispatch_dedup WHERE dedup_key = :k"
            ),
            {"k": dedup_key},
        ).first()
    return row is not None


def _seed_pr_opened_event(
    engine: Engine,
    *,
    repo: str = _REPO,
    pr_number: int = _PR_NUMBER,
    head_sha: str = _HEAD_SHA,
) -> None:
    """Insert a github.pr_opened event so the mergeability VIEW has a head_sha."""
    payload = json.dumps({"repo": repo, "pr_number": pr_number, "head_sha": head_sha})
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO events (entity_type, action, payload) "
                "VALUES ('github', 'pr_opened', :p::jsonb)"
            ),
            {"p": payload},
        )


def _seed_review_completed(
    engine: Engine,
    task_id: uuid.UUID,
    wv_id: uuid.UUID,
    *,
    head_sha: str = _HEAD_SHA,
    decision: str = "approved",
) -> None:
    """Seed a wf-review step already completed with the given decision."""
    output = json.dumps(
        {
            "summary": f"review: {decision}",
            "decision": decision,
            "commit_sha": head_sha,
            "artifacts": [],
            "payload": {},
        }
    )
    with engine.begin() as conn:
        run_id: uuid.UUID = conn.execute(
            sa.text(
                "INSERT INTO workflow_runs (task_id, workflow_version_id, trigger) "
                "VALUES (:t, :wv, 'pr_opened') RETURNING id"
            ),
            {"t": task_id, "wv": wv_id},
        ).scalar()
        row = conn.execute(
            sa.text(
                "SELECT step_name, role_id FROM workflow_version_steps "
                "WHERE workflow_version_id = :wv AND step_index = 0"
            ),
            {"wv": wv_id},
        ).one()
        conn.execute(
            sa.text(
                "INSERT INTO workflow_run_steps "
                "(run_id, step_index, step_name, role_id, status, output, completed_at) "
                "VALUES (:r, 0, :name, :role, 'completed', :out::jsonb, now())"
            ),
            {"r": run_id, "name": row.step_name, "role": row.role_id, "out": output},
        )


def _seed_validate_pending(
    engine: Engine,
    task_id: uuid.UUID,
    wv_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a wf-validate run with a pending step; consumer will complete it."""
    return _make_run_with_step(
        engine, task_id, wv_id, trigger="pr_opened", step_status="pending"
    )


# ── Event builders ─────────────────────────────────────────────────────────────


def _step_failed_no_diff(step_id: uuid.UUID) -> dict[str, Any]:
    return {
        "entity_type": "step",
        "action": "failed",
        "step_id": str(step_id),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "failed_at": "2026-05-19T10:00:00+00:00",
            "error": "Claude Code produced no changes to commit",
        },
    }


def _step_completed_supersede(
    step_id: uuid.UUID,
    *,
    rewritten_description: str,
) -> dict[str, Any]:
    return {
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-19T10:01:00+00:00",
            "output": {
                "summary": "architect verdict: supersede",
                "decision": "supersede",
                "artifacts": [],
                "payload": {
                    "verdict": "supersede",
                    "reasoning": "Task description referenced wrong file paths.",
                    "rewritten_description": rewritten_description,
                    "dispatch": {
                        "workflow_id": None,
                        "intent": "supersede-rewrite-task",
                        "rewritten_description": rewritten_description,
                    },
                },
            },
        },
    }


def _step_completed_author_pr(
    step_id: uuid.UUID,
    *,
    pr_number: int = _PR_NUMBER,
    commit_sha: str = _HEAD_SHA,
    branch: str = _BRANCH,
) -> dict[str, Any]:
    return {
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-19T10:02:00+00:00",
            "output": {
                "summary": "branch pushed, PR opened",
                "decision": "pushed",
                "commit_sha": commit_sha,
                "artifacts": [
                    {"kind": "branch", "value": branch},
                    {
                        "kind": "pr_url",
                        "value": f"https://github.com/{_REPO}/pull/{pr_number}",
                    },
                ],
                "payload": {"pr_number": pr_number},
            },
        },
    }


def _step_completed_validate_pass(
    step_id: uuid.UUID,
    *,
    commit_sha: str = _HEAD_SHA,
) -> dict[str, Any]:
    return {
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-19T10:03:00+00:00",
            "output": {
                "summary": "all checks passed",
                "decision": "pass",
                "commit_sha": commit_sha,
                "artifacts": [],
                "payload": {"checks": []},
            },
        },
    }


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestSupersedeEndToEnd:
    @pytest.mark.asyncio
    async def test_full_supersede_flow(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        truncate: None,
        engine: Engine,
    ) -> None:
        """Full supersede chain: no-diff author → architect supersede →
        child task + fresh wf-author → PR authored → mergeability reached
        → auto-merge deadline set."""

        # ── Stage 1: seed parent task and initial wf-author run ───────────
        wv_ids = _seed_all_workflows(engine)
        plan_id = _make_plan(engine)
        parent_task_id = _make_task(
            engine,
            plan_id,
            wv_ids["wf-author"],
            title="Add feature X",
            description="Broken description that produces no diff.",
        )
        parent_author_run_id, parent_author_step_id = _make_run_with_step(
            engine,
            parent_task_id,
            wv_ids["wf-author"],
            trigger="registered",
            step_status="pending",
        )

        publisher = _RecordingPublisher()
        sqs = _RecordingSqs()
        consumer = _make_consumer(session_factory, publisher, sqs)

        # ── Stage 2: wf-author fails with the no-diff error ──────────────
        # The consumer routes step.failed for wf-author to
        # maybe_dispatch_architect_on_author_no_diff when the error
        # contains _NO_CHANGES_ERROR_SIGNATURE.
        await consumer.handle(_step_failed_no_diff(parent_author_step_id))

        # wf-architecture-resolve must have been dispatched for the parent task.
        arch_runs = _get_wf_runs_for_task(
            engine, parent_task_id, "wf-architecture-resolve"
        )
        assert len(arch_runs) == 1, (
            "expected 1 wf-architecture-resolve run after no-diff author failure"
        )
        _, arch_step_id = arch_runs[0]

        # Dedup key for the no-diff → architect dispatch must be locked.
        expected_arch_dedup = (
            f"wf-architecture-resolve:{_REPO}:author-no-diff-run={parent_author_run_id}"
        )
        assert _dedup_key_exists(engine, expected_arch_dedup), (
            "dedup key for wf-architecture-resolve not found after no-diff failure"
        )

        # SQS claim for the architect step must have been sent.
        arch_sqs = [
            json.loads(m["MessageBody"])
            for m in sqs.sent
            if json.loads(m["MessageBody"]).get("step_id") == str(arch_step_id)
        ]
        assert len(arch_sqs) == 1, "expected one SQS claim for the architect step"

        # ── Stage 3: parent has no open PR (no task_prs row) ─────────────
        # wf-author failed before opening a PR; the supersede trigger's
        # best-effort PR-close helper should skip cleanly.
        assert _get_task_prs(engine, parent_task_id) == [], (
            "parent task must not have a task_prs row (no PR was ever opened)"
        )

        # ── Stage 4: architect emits verdict=supersede ────────────────────
        # The consumer routes step.completed for wf-architecture-resolve to
        # maybe_dispatch_supersede_on_architect_verdict which creates the
        # child task, dispatches wf-author, and (best-effort) closes the PR.
        await consumer.handle(
            _step_completed_supersede(
                arch_step_id, rewritten_description=_REWRITTEN_DESC
            )
        )

        # Child task must be created with the rewritten description.
        child_rows = _get_child_tasks(engine, parent_task_id)
        assert len(child_rows) == 1, "expected exactly one child task after supersede"
        child = child_rows[0]
        assert child.parent_task_id == parent_task_id, (
            "child task must point back to parent via parent_task_id"
        )
        assert child.description == _REWRITTEN_DESC, (
            "child task must carry the architect's rewritten_description"
        )
        assert "(superseded)" in child.title, (
            "child task title must be suffixed with '(superseded)'"
        )
        assert child.created_by == "architect:supersede", (
            "child task created_by must be 'architect:supersede'"
        )
        child_task_id: uuid.UUID = child.id

        # Supersede dedup key must be locked to prevent duplicate children.
        expected_supersede_dedup = (
            f"wf-author:{_REPO}:supersede-parent={parent_task_id}"
        )
        assert _dedup_key_exists(engine, expected_supersede_dedup), (
            "dedup key for supersede wf-author dispatch not found"
        )

        # A fresh wf-author run must exist for the child task.
        child_author_runs = _get_wf_runs_for_task(engine, child_task_id, "wf-author")
        assert len(child_author_runs) == 1, (
            "expected 1 wf-author run for child task after supersede"
        )
        _, child_author_step_id = child_author_runs[0]

        # SQS claim for the child's author step must have been sent.
        child_sqs = [
            json.loads(m["MessageBody"])
            for m in sqs.sent
            if json.loads(m["MessageBody"]).get("step_id") == str(child_author_step_id)
        ]
        assert len(child_sqs) == 1, "expected SQS claim for child's wf-author step"

        # ── Stage 5: child's wf-author completes with a real PR artifact ──
        # Mocked: we drive the step.completed payload directly rather than
        # running Claude Code. The payload carries pr_number + branch so
        # _write_task_prs_on_completed can write the task_prs bridge row.
        await consumer.handle(_step_completed_author_pr(child_author_step_id))

        # task_prs row must be written for the child task.
        child_prs = _get_task_prs(engine, child_task_id)
        assert len(child_prs) == 1, (
            "expected task_prs row for child task after PR authored"
        )
        assert child_prs[0].pr_number == _PR_NUMBER
        assert child_prs[0].repo == _REPO
        assert child_prs[0].branch == _BRANCH

        # ── Stage 6: seed mergeability prerequisites ──────────────────────
        # Seed the GitHub pr_opened event so the mergeability VIEW resolves
        # head_sha for the (repo, pr_number) pair.
        _seed_pr_opened_event(engine)

        # Seed wf-review completed with decision=approved at HEAD.
        _seed_review_completed(engine, child_task_id, wv_ids["wf-review"])

        # Seed wf-validate as pending; the consumer will complete it in the
        # next stage which drives _maybe_fire_auto_merge within the same
        # transaction, allowing the VIEW to see the just-committed output.
        _, validate_step_id = _seed_validate_pending(
            engine, child_task_id, wv_ids["wf-validate"]
        )

        # ── Stage 7: drive wf-validate step.completed + auto-merge ───────
        # Wire a mock Redis so we can verify the deadline key is set without
        # needing a live Redis instance.
        redis = _mock_redis()
        consumer_with_redis = _make_consumer(
            session_factory,
            _RecordingPublisher(),
            _RecordingSqs(),
            redis_client=redis,
        )
        await consumer_with_redis.handle(
            _step_completed_validate_pass(validate_step_id)
        )

        # task_mergeability VIEW must show 'mergeable' for the child task.
        # (The VIEW sees: task_prs row, pr_opened event with head_sha,
        # wf-review approved at HEAD, wf-validate pass at HEAD.)
        mergeability = _get_mergeability(engine, child_task_id)
        assert mergeability == "mergeable", (
            f"expected child task to be mergeable after all signals seeded; "
            f"got {mergeability!r}"
        )

        # The auto-merge deadline key must have been set in Redis, proving
        # maybe_auto_merge_on_mergeable fired and scheduled the merge.
        redis.set.assert_awaited_once()
        deadline_key = redis.set.await_args[0][0]
        assert deadline_key.startswith(AUTO_MERGE_DEADLINE_KEY_PREFIX), (
            f"unexpected Redis key prefix: {deadline_key!r}"
        )
        assert str(child_task_id) in deadline_key, (
            f"deadline key {deadline_key!r} does not reference child task "
            f"{child_task_id}"
        )

        # Deadline payload must include the repo and PR number.
        deadline_value = json.loads(redis.set.await_args[0][1])
        assert deadline_value["repo"] == _REPO
        assert deadline_value["pr_number"] == _PR_NUMBER
        assert "deadline_at" in deadline_value
