"""Integration tests for the task_status VIEW.

Fixture-driven: each test seeds tasks with a specific event/step/PR
combination and asserts the resolved ``derived_status``. Covers every
priority category from ADR-0011's commitment to bunkhouse migration 020:

    cancelled > blocked > registered > <wf>: executing > pr_state / done

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest services/api/tests/test_integration_task_status.py
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

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


# ── Fixture builders ──────────────────────────────────────────────────────────


@pytest.fixture
def fixtures(engine: Engine) -> Iterator["FixtureBuilder"]:
    """Per-test fixture builder. Truncates the test surface before yielding
    and after teardown so each test starts from a known empty state."""
    builder = FixtureBuilder(engine)
    builder.truncate_all()
    try:
        yield builder
    finally:
        builder.truncate_all()


# Tables that fixture-driven tests touch. Order doesn't matter for TRUNCATE
# CASCADE — Postgres handles dependencies. We exclude alembic_version.
_TEST_TABLES = (
    "plans",
    "workflows",
    "workflow_versions",
    "workflow_version_steps",
    "tasks",
    "task_prs",
    "task_dependencies",
    "workflow_runs",
    "workflow_run_steps",
    "events",
    "roles",
    "skills",
    "hooks",
    "role_skills",
    "role_hooks",
    "event_triggers",
)


class FixtureBuilder:
    """Helper for creating Plan/Task/Workflow/Run/Step/Event/PR rows."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def truncate_all(self) -> None:
        """Wipe every test-touched table. ``CASCADE`` follows FKs."""
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(_TEST_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )

    def make_plan(self, repo: str = "test/repo") -> uuid.UUID:
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.text("INSERT INTO plans (repo) VALUES (:repo) RETURNING id"),
                {"repo": repo},
            ).one()
        return row.id

    def make_workflow_version(self, conn: Connection, slug: str) -> uuid.UUID:
        """Idempotent: registers the workflow + a v1 row if not already present."""
        existing = conn.execute(
            sa.text("SELECT id FROM workflow_versions WHERE workflow_id = :s AND version = 1"),
            {"s": slug},
        ).first()
        if existing:
            return existing.id
        conn.execute(
            sa.text(
                "INSERT INTO workflows (id) VALUES (:id) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": slug},
        )
        version_id = conn.execute(
            sa.text(
                "INSERT INTO workflow_versions (workflow_id, version) "
                "VALUES (:s, 1) RETURNING id"
            ),
            {"s": slug},
        ).scalar()
        return version_id

    def make_role(self, conn: Connection, role_id: str = "role-author") -> str:
        conn.execute(
            sa.text(
                "INSERT INTO roles (id, model, system_prompt, output_kind) "
                "VALUES (:id, 'claude', '', 'code') ON CONFLICT (id) DO NOTHING"
            ),
            {"id": role_id},
        )
        return role_id

    def make_task(
        self,
        plan_id: uuid.UUID,
        workflow_slug: str = "wf-author",
        repo: str = "test/repo",
        title: str = "test task",
    ) -> uuid.UUID:
        with self.engine.begin() as conn:
            wv_id = self.make_workflow_version(conn, workflow_slug)
            self.make_role(conn)
            task_id = conn.execute(
                sa.text(
                    "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
                    "VALUES (:plan_id, :repo, :title, :wv_id) RETURNING id"
                ),
                {"plan_id": plan_id, "repo": repo, "title": title, "wv_id": wv_id},
            ).scalar()
        return task_id

    def add_run_with_steps(
        self,
        task_id: uuid.UUID,
        workflow_slug: str,
        step_states: list[str],
        trigger: str = "registered",
        step_outputs: list[dict | None] | None = None,
    ) -> uuid.UUID:
        """Create a workflow_run with the given step statuses, in order.

        ``step_outputs``, when given, sets each step's ``output`` JSONB (e.g.
        ``[{"decision": "approved"}]``); ``None`` entries leave output NULL.
        """
        with self.engine.begin() as conn:
            wv_id = self.make_workflow_version(conn, workflow_slug)
            self.make_role(conn)
            run_id = conn.execute(
                sa.text(
                    "INSERT INTO workflow_runs (task_id, workflow_version_id, trigger) "
                    "VALUES (:t, :wv, :trig) RETURNING id"
                ),
                {"t": task_id, "wv": wv_id, "trig": trigger},
            ).scalar()
            for idx, state in enumerate(step_states):
                output = None
                if step_outputs is not None and idx < len(step_outputs):
                    output = step_outputs[idx]
                conn.execute(
                    sa.text(
                        "INSERT INTO workflow_run_steps "
                        "(run_id, step_index, step_name, role_id, status, output) "
                        "VALUES (:r, :i, :n, 'role-author', :s, "
                        "CAST(:o AS jsonb))"
                    ),
                    {
                        "r": run_id,
                        "i": idx,
                        "n": f"step-{idx}",
                        "s": state,
                        "o": _json_encode(output) if output is not None else None,
                    },
                )
        return run_id

    def add_event(
        self,
        task_id: uuid.UUID,
        entity_type: str,
        action: str,
        payload: dict | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO events (entity_type, action, task_id, payload) "
                    "VALUES (:et, :a, :t, CAST(:p AS jsonb))"
                ),
                {
                    "et": entity_type,
                    "a": action,
                    "t": task_id,
                    "p": _json_encode(payload or {}),
                },
            )

    def add_task_pr(
        self,
        task_id: uuid.UUID,
        repo: str = "test/repo",
        pr_number: int = 1,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO task_prs (repo, pr_number, task_id) "
                    "VALUES (:r, :p, :t)"
                ),
                {"r": repo, "p": pr_number, "t": task_id},
            )

    def add_dependency(self, task_id: uuid.UUID, expression: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO task_dependencies (task_id, expression) "
                    "VALUES (:t, :e)"
                ),
                {"t": task_id, "e": expression},
            )


def _json_encode(payload: dict) -> str:
    import json

    return json.dumps(payload)


def _status_for(engine: Engine, task_id: uuid.UUID) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT derived_status FROM task_status WHERE id = :id"),
            {"id": task_id},
        ).one_or_none()
    assert row is not None, f"task {task_id} not found in task_status VIEW"
    return row.derived_status


# ── Priority-category tests ───────────────────────────────────────────────────


def test_status_cancelled_wins_over_everything(engine: Engine, fixtures: FixtureBuilder) -> None:
    """A task.cancelled event flips status to 'cancelled' regardless of other state."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["completed"])
    fixtures.add_task_pr(task_id)
    fixtures.add_event(task_id, "github", "pr_merged")
    fixtures.add_event(task_id, "task", "cancelled")
    assert _status_for(engine, task_id) == "cancelled"


def test_status_blocked_with_unsatisfied_pr_merged_dependency(
    engine: Engine, fixtures: FixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    blocker_task_id = fixtures.make_task(plan_id, title="blocker")
    blocked_task_id = fixtures.make_task(plan_id, title="blocked")
    fixtures.add_dependency(blocked_task_id, f"task.{blocker_task_id}.pr_merged")
    assert _status_for(engine, blocked_task_id) == "blocked"


def test_status_unblocks_when_dependency_pr_merged(
    engine: Engine, fixtures: FixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    blocker_task_id = fixtures.make_task(plan_id, title="blocker")
    blocked_task_id = fixtures.make_task(plan_id, title="blocked")
    fixtures.add_dependency(blocked_task_id, f"task.{blocker_task_id}.pr_merged")
    fixtures.add_event(blocker_task_id, "github", "pr_merged")
    # No longer blocked; falls through to 'registered' (no runs).
    assert _status_for(engine, blocked_task_id) == "registered"


def test_status_registered_when_no_runs_no_deps(
    engine: Engine, fixtures: FixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    assert _status_for(engine, task_id) == "registered"


def test_status_executing_with_workflow_prefix(
    engine: Engine, fixtures: FixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["running"])
    assert _status_for(engine, task_id) == "wf-author: executing"


def test_status_executing_with_pending_step(
    engine: Engine, fixtures: FixtureBuilder,
) -> None:
    """Pending steps count as active (not yet started but reserved for the run)."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["completed", "pending"])
    assert _status_for(engine, task_id) == "wf-author: executing"


def test_status_failed_no_pr(engine: Engine, fixtures: FixtureBuilder) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["failed"])
    assert _status_for(engine, task_id) == "wf-author: failed"


def test_status_pr_opened_failed_overlay(engine: Engine, fixtures: FixtureBuilder) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["failed"])
    fixtures.add_task_pr(task_id)
    assert _status_for(engine, task_id) == "pr_opened (wf-author: failed)"


def test_status_pr_merged_failed_overlay(engine: Engine, fixtures: FixtureBuilder) -> None:
    """A failure that occurs after pr_merged keeps the merge context visible."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-review", ["failed"])
    fixtures.add_task_pr(task_id)
    fixtures.add_event(task_id, "github", "pr_merged")
    assert _status_for(engine, task_id) == "pr_merged (wf-review: failed)"


def test_status_pr_opened(engine: Engine, fixtures: FixtureBuilder) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["completed"])
    fixtures.add_task_pr(task_id)
    assert _status_for(engine, task_id) == "pr_opened"


def test_status_pr_merged(engine: Engine, fixtures: FixtureBuilder) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["completed"])
    fixtures.add_task_pr(task_id)
    fixtures.add_event(task_id, "github", "pr_merged")
    assert _status_for(engine, task_id) == "pr_merged"


def test_status_merged_via_review_is_pr_merged(
    engine: Engine, fixtures: FixtureBuilder,
) -> None:
    """A merged task whose latest run is wf-review derives 'pr_merged', not
    'review_passed'. pr_merged is authoritative whenever a merge event exists —
    auto-merge creates no later run, so the happy path lands here. (Regression
    guard for the 20260520_0500 precedence fix.)"""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["completed"])
    fixtures.add_task_pr(task_id)
    fixtures.add_event(task_id, "github", "pr_merged")
    fixtures.add_run_with_steps(
        task_id, "wf-review", ["completed"],
        step_outputs=[{"decision": "approved"}],
    )
    assert _status_for(engine, task_id) == "pr_merged"


def test_status_review_passed_is_pre_merge(
    engine: Engine, fixtures: FixtureBuilder,
) -> None:
    """review_passed is the PRE-merge state: open PR, no merge event, latest run
    wf-review with a completed step decision='approved' (the auto-merge
    cooling-off window)."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["completed"])
    fixtures.add_task_pr(task_id)
    fixtures.add_run_with_steps(
        task_id, "wf-review", ["completed"],
        step_outputs=[{"decision": "approved"}],
    )
    assert _status_for(engine, task_id) == "review_passed"


def test_status_pr_opened_when_review_not_yet_approved(
    engine: Engine, fixtures: FixtureBuilder,
) -> None:
    """An open PR whose latest wf-review run has no approved decision is
    'pr_opened', not 'review_passed'."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["completed"])
    fixtures.add_task_pr(task_id)
    fixtures.add_run_with_steps(task_id, "wf-review", ["completed"])
    assert _status_for(engine, task_id) == "pr_opened"


def test_status_done_when_no_pr(engine: Engine, fixtures: FixtureBuilder) -> None:
    """A task whose runs all completed and that never had a PR shows 'done'."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_run_with_steps(task_id, "wf-author", ["completed"])
    assert _status_for(engine, task_id) == "done"
