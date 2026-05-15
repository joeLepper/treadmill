"""Integration tests for the ``event_triggers`` evaluator (Week-3 C.2).

Per ADR-0007 §"GitHub webhook ingestion" + Week-3 plan §C.2, the
coordination consumer hands each github event to the trigger evaluator
(``coordination/triggers.py``); the evaluator looks up matching rows in
``event_triggers``, applies per-event filters + cap policies, and
creates a fresh ``WorkflowRun`` for each matched workflow.

These tests drive ``CoordinationConsumer.handle()`` against a live
Postgres so the table joins (catch-all vs repo-specific) + cap-policy
counts run exactly as production.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest tests/test_integration_event_triggers.py
"""

from __future__ import annotations

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

from treadmill_api.coordination import CoordinationConsumer
from treadmill_api.dispatch import Dispatcher

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


# ── Test doubles ──────────────────────────────────────────────────────────────


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


# ── Seeding helpers ───────────────────────────────────────────────────────────


# All workflow ids referenced by the trigger mappings. We seed every
# workflow + a v1 version + one step (role-code-author) so the run
# creation path has a workflow_version_id + at least one step to
# materialize.
_WORKFLOW_DEFS: list[tuple[str, str]] = [
    # (workflow_id, single_role_for_step1)
    ("wf-author", "role-code-author"),
    ("wf-review", "role-reviewer"),
    ("wf-validate", "role-validator"),
    ("wf-feedback", "role-feedback-analyzer"),
    ("wf-ci-fix", "role-ci-analyzer"),
    ("wf-conflict", "role-conflict-analyzer"),
    ("wf-plan", "role-planner"),
]


_TRIGGER_MAPPINGS: list[tuple[str, str]] = [
    ("pr_opened", "wf-review"),
    ("pr_synchronize", "wf-review"),
    ("pr_review_submitted", "wf-feedback"),
    ("check_run_completed", "wf-ci-fix"),
    ("pr_conflict", "wf-conflict"),
]


def _seed_world(
    engine: Engine,
    *,
    repo: str = "acme/myapp",
    pr_number: int = 42,
    seed_triggers: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Seed the workflow catalog + a task with a PR. Returns
    ``(task_id, plan_id, workflow_version_ids_by_workflow_id)``.

    By default also seeds the five catch-all ``event_triggers`` rows so
    individual tests don't have to. Tests that need a different trigger
    config (e.g. repo-specific override) pass ``seed_triggers=False``
    and seed manually.
    """
    wv_ids: dict[str, uuid.UUID] = {}
    with engine.begin() as conn:
        # Roles.
        for role_id in {r for _, r in _WORKFLOW_DEFS}:
            conn.execute(sa.text(
                "INSERT INTO roles (id, model, system_prompt, output_kind) "
                "VALUES (:r, 'claude', '', 'code') ON CONFLICT DO NOTHING"
            ), {"r": role_id})

        # Workflows + v1 versions + one step each.
        for workflow_id, role_id in _WORKFLOW_DEFS:
            conn.execute(sa.text(
                "INSERT INTO workflows (id) VALUES (:w) ON CONFLICT DO NOTHING"
            ), {"w": workflow_id})
            wv_id = conn.execute(sa.text(
                "INSERT INTO workflow_versions (workflow_id, version) "
                "VALUES (:w, 1) RETURNING id"
            ), {"w": workflow_id}).scalar()
            wv_ids[workflow_id] = wv_id
            conn.execute(sa.text(
                "INSERT INTO workflow_version_steps "
                "(workflow_version_id, step_index, step_name, role_id) "
                "VALUES (:wv, 0, :name, :role)"
            ), {"wv": wv_id, "name": "step-0", "role": role_id})

        # Trigger rows (catch-all).
        if seed_triggers:
            for event_type, workflow_id in _TRIGGER_MAPPINGS:
                conn.execute(sa.text(
                    "INSERT INTO event_triggers "
                    "(repo, event_type, workflow_id, version_strategy, enabled) "
                    "VALUES (NULL, :et, :w, 'latest', TRUE) "
                    "ON CONFLICT DO NOTHING"
                ), {"et": event_type, "w": workflow_id})

        # Plan + task pinned to wf-author's version (arbitrary — the
        # task's own version pin is unrelated to which workflow the
        # trigger evaluator dispatches).
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES (:repo) RETURNING id"
        ), {"repo": repo}).scalar()
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, plan_id, payload) "
            "VALUES ('plan', 'activated', :p, '{}'::jsonb)"
        ), {"p": plan_id})
        task_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, :repo, 't', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv_ids["wf-author"], "repo": repo}).scalar()
        # task_prs bridge so the trigger evaluator can resolve the task.
        conn.execute(sa.text(
            "INSERT INTO task_prs (repo, pr_number, task_id, branch) "
            "VALUES (:repo, :pr, :t, 'task/foo')"
        ), {"repo": repo, "pr": pr_number, "t": task_id})

    return task_id, plan_id, wv_ids


def _github_event_record(
    *,
    action: str,
    repo: str = "acme/myapp",
    pr_number: int = 42,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a coordination-consumer-shaped event record for a github
    verb. ``extras`` merges into the payload.

    Each verb has a slightly different payload schema (per
    ``events/github.py``); the field set below is the minimum each
    registered Pydantic class requires. We don't union them — each
    branch builds exactly what its target class accepts.
    """
    base: dict[str, Any] = {"repo": repo, "pr_number": pr_number}
    if action == "pr_opened":
        payload = {
            **base, "sender": "alice", "title": "feat: x",
            "head_branch": "task/foo", "head_sha": "a" * 40,
        }
    elif action == "pr_synchronize":
        payload = {**base, "sender": "alice", "head_sha": "b" * 40}
    elif action == "pr_review_submitted":
        payload = {
            **base, "sender": "alice",
            "state": (extras or {}).get("state", "changes_requested"),
        }
    elif action == "check_run_completed":
        payload = {
            **base, "check_name": "tests",
            "conclusion": (extras or {}).get("conclusion", "failure"),
            "head_sha": "c" * 40,
        }
    elif action == "pr_conflict":
        payload = {**base, "head_sha": "d" * 40, "is_conflicting": True}
    elif action == "pr_merged":
        payload = {**base, "sender": "alice", "merged_sha": "e" * 40}
    else:
        payload = {**base}
    # ``extras`` overrides any default fields above (e.g. the ``state``
    # override for pr_review_submitted, ``conclusion`` for check_run).
    # Apply last so the caller wins.
    if extras:
        payload.update(extras)
    return {
        "entity_type": "github",
        "action": action,
        "event_id": str(uuid.uuid4()),
        "task_id": None,  # left for the evaluator's bridge lookup
        "payload": payload,
    }


def _make_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    publisher: _RecordingPublisher,
    sqs: _RecordingSqs,
) -> CoordinationConsumer:
    """Build a consumer with a dispatcher pointed at the recording fakes.

    ``github_client`` is left ``None`` so the conflict sweep is skipped
    on ``pr_merged`` (these tests are about the trigger evaluator, not
    the sweep)."""
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
        publisher=publisher,
    )


def _count_runs_for_workflow(
    engine: Engine, task_id: uuid.UUID, workflow_id: str,
) -> int:
    """Count ``WorkflowRun`` rows for ``(task, workflow_id)``."""
    with engine.connect() as conn:
        return conn.execute(sa.text(
            """
            SELECT COUNT(*) FROM workflow_runs wr
            JOIN workflow_versions wv ON wv.id = wr.workflow_version_id
            WHERE wr.task_id = :t AND wv.workflow_id = :w
            """
        ), {"t": task_id, "w": workflow_id}).scalar()


def _step_completed_event_record(
    *,
    step_id: str | uuid.UUID = "",
    decision: str = "pass",
    workflow_id: str = "wf-validate",
) -> dict[str, Any]:
    """Build a coordination-consumer-shaped step.completed event record.

    Used for testing self-triggered workflows like wf-validate → wf-feedback.
    """
    if not step_id:
        step_id = str(uuid.uuid4())
    return {
        "entity_type": "step",
        "action": "completed",
        "step_id": str(step_id),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-08T10:00:00+00:00",
            "output": {
                "summary": f"validation {decision}",
                "decision": decision,
                "commit_sha": "abc123",
                "artifacts": [],
                "payload": {},
                "metadata": {},
            },
        },
    }


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_opened_fires_wf_review(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2 baseline: a ``pr_opened`` event with a catch-all trigger row
    fires a ``wf-review`` WorkflowRun against the task identified by
    ``task_prs``."""
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(action="pr_opened"))

    assert _count_runs_for_workflow(engine, task_id, "wf-review") == 1
    # No spurious runs for other workflows.
    for other in ("wf-author", "wf-validate", "wf-feedback", "wf-ci-fix"):
        assert _count_runs_for_workflow(engine, task_id, other) == 0


@pytest.mark.asyncio
async def test_pr_synchronize_fires_review_and_validate_concurrently(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2: per Week-3 plan + ADR-0013, a new HEAD invalidates prior
    review + validate thumbs. The evaluator dispatches both
    ``wf-review`` and ``wf-validate`` on every ``pr_synchronize``.

    The ``event_triggers`` table holds the ``wf-review`` mapping; the
    evaluator hardcodes the ``wf-validate`` fan-out for this event_type
    (see ``triggers.py:_EXTRA_FANOUT_WORKFLOWS``).
    """
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(action="pr_synchronize"))

    assert _count_runs_for_workflow(engine, task_id, "wf-review") == 1
    assert _count_runs_for_workflow(engine, task_id, "wf-validate") == 1


@pytest.mark.asyncio
async def test_pr_opened_fires_review_and_validate_concurrently(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """2026-05-15: extended fan-out so first-cycle PRs also run wf-validate.
    Before this, only ``pr_synchronize`` fanned out, which meant a PR
    opened once and never re-pushed had no ``validate_decision`` in the
    mergeability VIEW — auto-merge (ADR-0031) was structurally unable to
    fire on the first cycle. Surfaced by the first end-to-end smoke."""
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(action="pr_opened"))

    assert _count_runs_for_workflow(engine, task_id, "wf-review") == 1
    assert _count_runs_for_workflow(engine, task_id, "wf-validate") == 1


@pytest.mark.asyncio
async def test_pr_review_submitted_changes_requested_fires_feedback(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2: only ``state='changes_requested'`` fires ``wf-feedback``.
    An approval is the human signing off, not a problem to solve."""
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(
        action="pr_review_submitted",
        extras={"state": "changes_requested"},
    ))

    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 1


@pytest.mark.asyncio
async def test_pr_review_submitted_approved_does_not_fire_feedback(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2 negative: ``state='approved'`` (and ``commented``) are
    filtered out before any workflow fires. The per-event filter in
    ``triggers.py:_event_passes_filter`` is the gate."""
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(
        action="pr_review_submitted",
        extras={"state": "approved"},
    ))

    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 0


@pytest.mark.asyncio
async def test_check_run_completed_failure_fires_ci_fix(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2: a failing CI check fires ``wf-ci-fix``."""
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(
        action="check_run_completed",
        extras={"conclusion": "failure"},
    ))

    assert _count_runs_for_workflow(engine, task_id, "wf-ci-fix") == 1


@pytest.mark.asyncio
async def test_check_run_completed_success_does_not_fire_ci_fix(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2 negative: a passing CI check is not a problem to solve. The
    ``FAILURE_CONCLUSIONS`` frozenset in ``triggers.py`` excludes
    ``success``/``neutral``/``cancelled``/etc."""
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(
        action="check_run_completed",
        extras={"conclusion": "success"},
    ))

    assert _count_runs_for_workflow(engine, task_id, "wf-ci-fix") == 0


@pytest.mark.asyncio
async def test_pr_conflict_fires_wf_conflict(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2: a ``pr_conflict`` emission (from the conflict sweep)
    fires ``wf-conflict`` against the affected task."""
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(action="pr_conflict"))

    assert _count_runs_for_workflow(engine, task_id, "wf-conflict") == 1


@pytest.mark.asyncio
async def test_ci_fix_caps_at_three_attempts(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2 cap policy: per ADR-0015 Q15.b, ``wf-ci-fix`` caps at 3
    attempts per task. The fourth ``check_run_completed.failure`` event
    must not create a new ``wf-ci-fix`` run."""
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    # First three failures land three runs.
    for _ in range(3):
        await consumer.handle(_github_event_record(
            action="check_run_completed",
            extras={"conclusion": "failure"},
        ))
    assert _count_runs_for_workflow(engine, task_id, "wf-ci-fix") == 3

    # Fourth failure is rejected by the cap; count stays at 3.
    await consumer.handle(_github_event_record(
        action="check_run_completed",
        extras={"conclusion": "failure"},
    ))
    assert _count_runs_for_workflow(engine, task_id, "wf-ci-fix") == 3


@pytest.mark.asyncio
async def test_conflict_caps_at_three_attempts(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2 cap policy: ``wf-conflict`` caps at 3 attempts per task.
    Same shape as the ci-fix test."""
    task_id, _, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    for _ in range(3):
        await consumer.handle(_github_event_record(action="pr_conflict"))
    assert _count_runs_for_workflow(engine, task_id, "wf-conflict") == 3

    await consumer.handle(_github_event_record(action="pr_conflict"))
    assert _count_runs_for_workflow(engine, task_id, "wf-conflict") == 3


@pytest.mark.asyncio
async def test_disabled_trigger_does_not_fire(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2: ``event_triggers.enabled=FALSE`` rows are skipped by the
    evaluator's WHERE clause. Operators can park a trigger without
    deleting it; the row stays for audit but produces no dispatches.

    ``pr_synchronize`` is a good case because it has a hardcoded
    fan-out for ``wf-validate`` AS WELL AS the table-driven
    ``wf-review`` row. Disabling the row should suppress ``wf-review``
    but still let ``wf-validate`` fire (the hardcoded fan-out is
    independent of the table — Treadmill's design choice, documented
    in ``triggers.py``)."""
    task_id, _, _ = _seed_world(engine, seed_triggers=False)
    # Seed every catch-all trigger; then disable the pr_opened one.
    with engine.begin() as conn:
        for event_type, workflow_id in _TRIGGER_MAPPINGS:
            enabled = event_type != "pr_opened"
            conn.execute(sa.text(
                "INSERT INTO event_triggers "
                "(repo, event_type, workflow_id, version_strategy, enabled) "
                "VALUES (NULL, :et, :w, 'latest', :en)"
            ), {"et": event_type, "w": workflow_id, "en": enabled})
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(action="pr_opened"))
    assert _count_runs_for_workflow(engine, task_id, "wf-review") == 0


@pytest.mark.asyncio
async def test_repo_specific_trigger_overrides_catchall(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2: repo-specific rows take precedence over catch-all rows for
    the same ``event_type``. We seed a catch-all
    ``pr_opened → wf-review`` plus a repo-specific
    ``pr_opened → wf-author`` for ``acme/myapp``; only ``wf-author``
    fires.

    The bunkhouse precedent (``triggers.py:TriggerEvaluator.evaluate``)
    is the same — repo-specific wins."""
    task_id, _, _ = _seed_world(engine, seed_triggers=False)
    with engine.begin() as conn:
        # Catch-all row.
        conn.execute(sa.text(
            "INSERT INTO event_triggers "
            "(repo, event_type, workflow_id, version_strategy, enabled) "
            "VALUES (NULL, 'pr_opened', 'wf-review', 'latest', TRUE)"
        ))
        # Repo-specific override pointing at a different workflow.
        conn.execute(sa.text(
            "INSERT INTO event_triggers "
            "(repo, event_type, workflow_id, version_strategy, enabled) "
            "VALUES ('acme/myapp', 'pr_opened', 'wf-author', 'latest', TRUE)"
        ))
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(action="pr_opened"))

    # wf-author fires; wf-review does not.
    assert _count_runs_for_workflow(engine, task_id, "wf-author") == 1
    assert _count_runs_for_workflow(engine, task_id, "wf-review") == 0


@pytest.mark.asyncio
async def test_event_without_task_prs_bridge_does_not_fire(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2: when the task_prs bridge has no row for (repo, pr_number),
    the evaluator can't resolve a task and drops the event. Per
    ADR-0007's cache-then-heal pattern, the webhook receiver buffers
    these events in Redis for replay; the evaluator just no-ops here.
    """
    _seed_world(engine, seed_triggers=True)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    # Send a pr_opened for a PR that doesn't exist in task_prs.
    await consumer.handle(_github_event_record(
        action="pr_opened",
        pr_number=9999,
    ))

    with engine.connect() as conn:
        wr_count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM workflow_runs"
        )).scalar()
    assert wr_count == 0


@pytest.mark.asyncio
async def test_step_ready_published_for_dispatched_run(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """C.2 plumbing: when the evaluator creates a run, it must
    publish ``step.ready`` for step 1 + send the SQS work-queue
    claim. The downstream worker reads both."""
    task_id, plan_id, _ = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    await consumer.handle(_github_event_record(action="pr_opened"))

    # Step.ready published with the dispatched workflow.
    ready_calls = [
        (event, payload) for event, payload in publisher.calls
        if event.action == "ready"
    ]
    assert len(ready_calls) == 1
    _, payload = ready_calls[0]
    assert payload.workflow_id == "wf-review"

    # SQS claim sent for the same run.
    assert len(sqs.sent) == 1


# ── wf-validate → wf-feedback convergence trigger (ADR-0029) ─────────────────


@pytest.mark.asyncio
async def test_wf_validate_fail_fires_wf_feedback(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """ADR-0029 convergence trigger: when a ``wf-validate.step.completed``
    arrives with ``decision='fail'``, the consumer must dispatch ``wf-feedback``
    to analyze the failure and provide a task directive for re-authoring."""
    task_id, _, wv_ids = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    # Create a wf-validate run + step so the completion event finds a
    # valid step to update.
    with engine.begin() as conn:
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs "
            "(task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'pr_synchronize') "
            "RETURNING id"
        ), {"t": task_id, "wv": wv_ids["wf-validate"]}).scalar()
        step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status) "
            "VALUES (:r, 0, 'step-0', 'role-validator', 'running') "
            "RETURNING id"
        ), {"r": run_id}).scalar()

    # Send a wf-validate completion with decision='fail'.
    await consumer.handle(_step_completed_event_record(
        step_id=step_id,
        decision="fail",
        workflow_id="wf-validate",
    ))

    # Verify wf-feedback was dispatched.
    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 1


@pytest.mark.asyncio
async def test_wf_validate_pass_does_not_fire_wf_feedback(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """ADR-0029: when a ``wf-validate.step.completed`` arrives with
    ``decision='pass'``, wf-feedback must NOT be dispatched."""
    task_id, _, wv_ids = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    with engine.begin() as conn:
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs "
            "(task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'pr_synchronize') "
            "RETURNING id"
        ), {"t": task_id, "wv": wv_ids["wf-validate"]}).scalar()
        step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status) "
            "VALUES (:r, 0, 'step-0', 'role-validator', 'running') "
            "RETURNING id"
        ), {"r": run_id}).scalar()

    await consumer.handle(_step_completed_event_record(
        step_id=step_id,
        decision="pass",
        workflow_id="wf-validate",
    ))

    # Verify wf-feedback was NOT dispatched.
    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 0


@pytest.mark.asyncio
async def test_wf_feedback_caps_at_five_attempts(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """ADR-0029 Q29.e: wf-feedback caps at 5 attempts per task across
    all trigger sources. The 6th wf-feedback dispatch must be rejected."""
    task_id, _, wv_ids = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    with engine.begin() as conn:
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs "
            "(task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'pr_synchronize') "
            "RETURNING id"
        ), {"t": task_id, "wv": wv_ids["wf-validate"]}).scalar()
        step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status) "
            "VALUES (:r, 0, 'step-0', 'role-validator', 'running') "
            "RETURNING id"
        ), {"r": run_id}).scalar()

    # First five wf-validate failures each dispatch wf-feedback.
    for i in range(5):
        # Create a new wf-validate run for each attempt
        with engine.begin() as conn:
            new_run_id = conn.execute(sa.text(
                "INSERT INTO workflow_runs "
                "(task_id, workflow_version_id, trigger) "
                "VALUES (:t, :wv, 'pr_synchronize') "
                "RETURNING id"
            ), {"t": task_id, "wv": wv_ids["wf-validate"]}).scalar()
            new_step_id = conn.execute(sa.text(
                "INSERT INTO workflow_run_steps "
                "(run_id, step_index, step_name, role_id, status) "
                "VALUES (:r, 0, 'step-0', 'role-validator', 'running') "
                "RETURNING id"
            ), {"r": new_run_id}).scalar()

        await consumer.handle(_step_completed_event_record(
            step_id=new_step_id,
            decision="fail",
        ))

    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 5

    # 6th failure is rejected by the cap; wf-feedback count stays at 5.
    with engine.begin() as conn:
        final_run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs "
            "(task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'pr_synchronize') "
            "RETURNING id"
        ), {"t": task_id, "wv": wv_ids["wf-validate"]}).scalar()
        final_step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status) "
            "VALUES (:r, 0, 'step-0', 'role-validator', 'running') "
            "RETURNING id"
        ), {"r": final_run_id}).scalar()

    await consumer.handle(_step_completed_event_record(
        step_id=final_step_id,
        decision="fail",
    ))

    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 5


# ── wf-author failure → wf-feedback convergence trigger (ADR-0037) ─────────────


@pytest.mark.asyncio
async def test_wf_author_fail_fires_wf_feedback(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """ADR-0037 convergence trigger: when a ``wf-author.step.completed``
    arrives with ``decision='fail'``, the consumer must dispatch ``wf-feedback``
    to analyze the failure and provide a task directive for re-authoring."""
    task_id, _, wv_ids = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    # Create a wf-author run + step so the completion event finds a
    # valid step to update.
    with engine.begin() as conn:
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs "
            "(task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'manual') "
            "RETURNING id"
        ), {"t": task_id, "wv": wv_ids["wf-author"]}).scalar()
        step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status) "
            "VALUES (:r, 0, 'step-0', 'role-code-author', 'running') "
            "RETURNING id"
        ), {"r": run_id}).scalar()

    # Send a wf-author completion with decision='fail'.
    await consumer.handle(_step_completed_event_record(
        step_id=step_id,
        decision="fail",
        workflow_id="wf-author",
    ))

    # Verify wf-feedback was dispatched.
    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 1


@pytest.mark.asyncio
async def test_wf_author_pass_does_not_fire_wf_feedback(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """ADR-0037: when a ``wf-author.step.completed`` arrives with
    ``decision='pass'``, wf-feedback must NOT be dispatched."""
    task_id, _, wv_ids = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    with engine.begin() as conn:
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs "
            "(task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'manual') "
            "RETURNING id"
        ), {"t": task_id, "wv": wv_ids["wf-author"]}).scalar()
        step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status) "
            "VALUES (:r, 0, 'step-0', 'role-code-author', 'running') "
            "RETURNING id"
        ), {"r": run_id}).scalar()

    await consumer.handle(_step_completed_event_record(
        step_id=step_id,
        decision="pass",
        workflow_id="wf-author",
    ))

    # Verify wf-feedback was NOT dispatched.
    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 0


@pytest.mark.asyncio
async def test_wf_author_fail_shares_wf_feedback_cap(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """ADR-0037 + ADR-0029 Q29.e: wf-author failure shares the 5-attempt
    cap with other wf-feedback sources (wf-validate, wf-review).
    Dispatches from different sources all count toward the same cap."""
    task_id, _, wv_ids = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    # First: dispatch 2 wf-feedback from wf-validate failures
    for i in range(2):
        with engine.begin() as conn:
            run_id = conn.execute(sa.text(
                "INSERT INTO workflow_runs "
                "(task_id, workflow_version_id, trigger) "
                "VALUES (:t, :wv, 'pr_synchronize') "
                "RETURNING id"
            ), {"t": task_id, "wv": wv_ids["wf-validate"]}).scalar()
            step_id = conn.execute(sa.text(
                "INSERT INTO workflow_run_steps "
                "(run_id, step_index, step_name, role_id, status) "
                "VALUES (:r, 0, 'step-0', 'role-validator', 'running') "
                "RETURNING id"
            ), {"r": run_id}).scalar()
        await consumer.handle(_step_completed_event_record(
            step_id=step_id,
            decision="fail",
            workflow_id="wf-validate",
        ))

    # Then: dispatch 3 wf-feedback from wf-author failures
    for i in range(3):
        with engine.begin() as conn:
            run_id = conn.execute(sa.text(
                "INSERT INTO workflow_runs "
                "(task_id, workflow_version_id, trigger) "
                "VALUES (:t, :wv, 'manual') "
                "RETURNING id"
            ), {"t": task_id, "wv": wv_ids["wf-author"]}).scalar()
            step_id = conn.execute(sa.text(
                "INSERT INTO workflow_run_steps "
                "(run_id, step_index, step_name, role_id, status) "
                "VALUES (:r, 0, 'step-0', 'role-code-author', 'running') "
                "RETURNING id"
            ), {"r": run_id}).scalar()
        await consumer.handle(_step_completed_event_record(
            step_id=step_id,
            decision="fail",
            workflow_id="wf-author",
        ))

    # Total should be 5 (2 + 3).
    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 5

    # 6th attempt from wf-author should be rejected by the cap.
    with engine.begin() as conn:
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs "
            "(task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'manual') "
            "RETURNING id"
        ), {"t": task_id, "wv": wv_ids["wf-author"]}).scalar()
        step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status) "
            "VALUES (:r, 0, 'step-0', 'role-code-author', 'running') "
            "RETURNING id"
        ), {"r": run_id}).scalar()

    await consumer.handle(_step_completed_event_record(
        step_id=step_id,
        decision="fail",
        workflow_id="wf-author",
    ))

    # Still 5, cap enforced.
    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 5


@pytest.mark.asyncio
async def test_wf_author_fail_dedup_prevents_double_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
    truncate: None,
    engine: Engine,
) -> None:
    """ADR-0037 + ADR-0026: dedup namespace 'author-fail-run' prevents
    double-dispatch on redelivery of the same wf-author completion event."""
    task_id, _, wv_ids = _seed_world(engine)
    publisher = _RecordingPublisher()
    sqs = _RecordingSqs()
    consumer = _make_consumer(session_factory, publisher, sqs)

    with engine.begin() as conn:
        run_id = conn.execute(sa.text(
            "INSERT INTO workflow_runs "
            "(task_id, workflow_version_id, trigger) "
            "VALUES (:t, :wv, 'manual') "
            "RETURNING id"
        ), {"t": task_id, "wv": wv_ids["wf-author"]}).scalar()
        step_id = conn.execute(sa.text(
            "INSERT INTO workflow_run_steps "
            "(run_id, step_index, step_name, role_id, status) "
            "VALUES (:r, 0, 'step-0', 'role-code-author', 'running') "
            "RETURNING id"
        ), {"r": run_id}).scalar()

    record = _step_completed_event_record(
        step_id=step_id,
        decision="fail",
        workflow_id="wf-author",
    )

    # First delivery should dispatch wf-feedback.
    await consumer.handle(record)
    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 1

    # Redelivery of the same step.completed event with a new step status
    # (idempotent update) should NOT dispatch again due to dedup.
    # First, verify the step is already in 'completed' state.
    with engine.connect() as conn:
        step_status = conn.execute(sa.text(
            "SELECT status FROM workflow_run_steps WHERE id = :s"
        ), {"s": step_id}).scalar()
    assert step_status == "completed"

    # Redelivery should hit the dedup gate and skip.
    await consumer.handle(record)
    assert _count_runs_for_workflow(engine, task_id, "wf-feedback") == 1
