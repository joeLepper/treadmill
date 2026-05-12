"""Integration tests for the task_mergeability VIEW (ADR-0013).

Fixture-driven: each test seeds events + workflow_run_steps + task_prs
in shapes that exercise one priority slot in the VIEW's CASE-WHEN. The
twelve required cases mirror ADR-0013 §"Derived states" plus the
per-commit invalidation case (push new HEAD → mergeable falls back to
pending).

Also includes a router smoke for ``GET /api/v1/tasks/{id}/mergeability``.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest services/api/tests/test_integration_task_mergeability.py
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
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
DEFAULT_API_URL = "http://localhost:8088"


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def api_url() -> str:
    return os.environ.get("TREADMILL_API_URL", DEFAULT_API_URL)


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


@pytest.fixture
def fixtures(engine: Engine) -> Iterator["MergeabilityFixtureBuilder"]:
    builder = MergeabilityFixtureBuilder(engine)
    builder.truncate_all()
    try:
        yield builder
    finally:
        builder.truncate_all()


class MergeabilityFixtureBuilder:
    """Seeds the minimum schema each mergeability test needs.

    The VIEW reads four signal kinds at HEAD:

      * ``head_sha`` from the latest ``github.pr_opened`` /
        ``pr_synchronize`` for ``(repo, pr_number)``.
      * ``wf-review`` latest completed step at HEAD (envelope's
        ``commit_sha`` matches ``head_sha``).
      * ``wf-validate`` latest completed step at HEAD.
      * ``github.check_run_completed`` events at HEAD (aggregated).
      * ``github.pr_conflict`` latest event at HEAD with
        ``is_conflicting`` payload.

    Each helper writes one of these surfaces deterministically.
    """

    DEFAULT_REPO = "test/merge"
    DEFAULT_PR = 42

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def truncate_all(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(_TEST_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )

    # ── basic schema setup ───────────────────────────────────────────

    def make_plan(self, repo: str = DEFAULT_REPO) -> uuid.UUID:
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.text("INSERT INTO plans (repo) VALUES (:repo) RETURNING id"),
                {"repo": repo},
            ).one()
        return row.id

    def make_workflow_version(self, conn: Connection, slug: str) -> uuid.UUID:
        existing = conn.execute(
            sa.text(
                "SELECT id FROM workflow_versions "
                "WHERE workflow_id = :s AND version = 1"
            ),
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
        return conn.execute(
            sa.text(
                "INSERT INTO workflow_versions (workflow_id, version) "
                "VALUES (:s, 1) RETURNING id"
            ),
            {"s": slug},
        ).scalar()

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
        repo: str = DEFAULT_REPO,
        title: str = "merge-test task",
    ) -> uuid.UUID:
        with self.engine.begin() as conn:
            wv_id = self.make_workflow_version(conn, workflow_slug)
            self.make_role(conn)
            task_id = conn.execute(
                sa.text(
                    "INSERT INTO tasks "
                    "(plan_id, repo, title, workflow_version_id) "
                    "VALUES (:p, :r, :t, :wv) RETURNING id"
                ),
                {"p": plan_id, "r": repo, "t": title, "wv": wv_id},
            ).scalar()
        return task_id

    def add_task_pr(
        self,
        task_id: uuid.UUID,
        repo: str = DEFAULT_REPO,
        pr_number: int = DEFAULT_PR,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO task_prs (repo, pr_number, task_id) "
                    "VALUES (:r, :p, :t)"
                ),
                {"r": repo, "p": pr_number, "t": task_id},
            )

    # ── signal: head ─────────────────────────────────────────────────

    def add_pr_opened(
        self,
        head_sha: str,
        repo: str = DEFAULT_REPO,
        pr_number: int = DEFAULT_PR,
    ) -> None:
        payload = {
            "repo": repo,
            "pr_number": pr_number,
            "sender": "tester",
            "title": "x",
            "head_branch": "feat/x",
            "head_sha": head_sha,
        }
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO events "
                    "(entity_type, action, commit_sha, payload) "
                    "VALUES ('github', 'pr_opened', :sha, CAST(:p AS jsonb))"
                ),
                {"sha": head_sha, "p": json.dumps(payload)},
            )

    def add_pr_synchronize(
        self,
        head_sha: str,
        before_sha: str | None = None,
        repo: str = DEFAULT_REPO,
        pr_number: int = DEFAULT_PR,
    ) -> None:
        payload = {
            "repo": repo,
            "pr_number": pr_number,
            "sender": "tester",
            "head_sha": head_sha,
            "before_sha": before_sha,
        }
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO events "
                    "(entity_type, action, commit_sha, payload) "
                    "VALUES ('github', 'pr_synchronize', :sha, "
                    "CAST(:p AS jsonb))"
                ),
                {"sha": head_sha, "p": json.dumps(payload)},
            )

    # ── signal: review / validate (workflow step envelopes) ──────────

    def add_step_envelope(
        self,
        task_id: uuid.UUID,
        workflow_slug: str,
        decision: str,
        commit_sha: str,
        role_id: str = "role-author",
    ) -> uuid.UUID:
        """Insert a completed workflow_run_step with a StepOutput envelope.

        The VIEW's LATERAL join filters on the envelope's top-level
        ``commit_sha`` (ADR-0012 promotion). We persist exactly the
        envelope shape — ``summary`` + ``decision`` + ``commit_sha`` —
        and fill in empty defaults for the rest.
        """
        envelope = {
            "summary": f"{workflow_slug} at {commit_sha}",
            "decision": decision,
            "commit_sha": commit_sha,
            "artifacts": [],
            "payload": {},
            "metadata": {},
        }
        with self.engine.begin() as conn:
            wv_id = self.make_workflow_version(conn, workflow_slug)
            self.make_role(conn, role_id)
            run_id = conn.execute(
                sa.text(
                    "INSERT INTO workflow_runs "
                    "(task_id, workflow_version_id, trigger) "
                    "VALUES (:t, :wv, 'webhook:test') RETURNING id"
                ),
                {"t": task_id, "wv": wv_id},
            ).scalar()
            step_id = conn.execute(
                sa.text(
                    "INSERT INTO workflow_run_steps "
                    "(run_id, step_index, step_name, role_id, status, "
                    " output, started_at, completed_at) "
                    "VALUES (:r, 0, :n, :role, 'completed', "
                    "CAST(:o AS jsonb), now(), now()) "
                    "RETURNING id"
                ),
                {
                    "r": run_id,
                    "n": workflow_slug,
                    "role": role_id,
                    "o": json.dumps(envelope),
                },
            ).scalar()
        return step_id

    # ── signal: ci ───────────────────────────────────────────────────

    def add_check_run(
        self,
        head_sha: str,
        conclusion: str,
        check_name: str = "ci",
        repo: str = DEFAULT_REPO,
        pr_number: int = DEFAULT_PR,
    ) -> None:
        """Insert a ``github.check_run_completed`` event at HEAD.

        ADR-0014 promotes ``commit_sha`` to a real column on
        ``events``; the VIEW joins on the column, not the payload.
        """
        payload = {
            "repo": repo,
            "pr_number": pr_number,
            "check_name": check_name,
            "conclusion": conclusion,
            "head_sha": head_sha,
        }
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO events "
                    "(entity_type, action, commit_sha, payload) "
                    "VALUES ('github', 'check_run_completed', "
                    ":sha, CAST(:p AS jsonb))"
                ),
                {"sha": head_sha, "p": json.dumps(payload)},
            )

    # ── signal: conflict ─────────────────────────────────────────────

    def add_pr_conflict(
        self,
        head_sha: str,
        is_conflicting: bool,
        repo: str = DEFAULT_REPO,
        pr_number: int = DEFAULT_PR,
    ) -> None:
        """Insert a ``github.pr_conflict`` event at HEAD.

        Agent 3 (Phase B.3) ships the ``GithubPrConflict`` event class
        + the sweep that emits it. For these tests we seed the row
        shape ADR-0013 commits to: ``commit_sha`` column populated +
        ``is_conflicting`` boolean in the payload.
        """
        payload = {
            "repo": repo,
            "pr_number": pr_number,
            "is_conflicting": is_conflicting,
        }
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO events "
                    "(entity_type, action, commit_sha, payload) "
                    "VALUES ('github', 'pr_conflict', :sha, "
                    "CAST(:p AS jsonb))"
                ),
                {"sha": head_sha, "p": json.dumps(payload)},
            )


def _mergeability_row(engine: Engine, task_id: uuid.UUID) -> sa.Row | None:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT * FROM task_mergeability WHERE task_id = :id"),
            {"id": task_id},
        ).one_or_none()


def _derived(engine: Engine, task_id: uuid.UUID) -> str:
    """Convenience: get the derived_mergeability for a task.

    The VIEW joins ``task_prs``, so a task with no PR row has no
    mergeability row at all. Callers expect a string — we surface that
    case as the literal ``'pending'`` to mirror the endpoint's behavior
    (the endpoint defaults missing rows to ``'pending'``)."""
    row = _mergeability_row(engine, task_id)
    if row is None:
        return "pending"
    return row.derived_mergeability


# ── Derived-state tests ───────────────────────────────────────────────


def test_mergeability_pending_when_no_pr(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    """Task exists, no ``task_prs`` row → VIEW has no row → ``pending``."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    assert _mergeability_row(engine, task_id) is None
    assert _derived(engine, task_id) == "pending"


def test_mergeability_pending_when_pr_opened_but_no_checks(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    """PR opened, HEAD known, but no review / validate / ci / conflict
    signals yet → falls through to the trailing ``else 'pending'``."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.head_sha == "sha-1"
    assert row.review_decision is None
    assert row.validate_decision is None
    assert row.ci_conclusion is None
    assert row.pr_conflicting is None
    assert row.derived_mergeability == "pending"


def test_mergeability_blocked_on_review_changes_requested(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_step_envelope(
        task_id, "wf-review", decision="changes_requested", commit_sha="sha-1",
    )
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.review_decision == "changes_requested"
    assert row.derived_mergeability == "blocked-on-review"


def test_mergeability_blocked_on_review_needs_more_info(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    """``needs-more-info`` also resolves to ``blocked-on-review`` per
    ADR-0013 §"Derived states", but the underlying ``review_decision``
    field preserves the distinction."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_step_envelope(
        task_id, "wf-review", decision="needs-more-info", commit_sha="sha-1",
    )
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.review_decision == "needs-more-info"
    assert row.derived_mergeability == "blocked-on-review"


def test_mergeability_blocked_on_validate_fail(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_step_envelope(
        task_id, "wf-validate", decision="fail", commit_sha="sha-1",
    )
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.validate_decision == "fail"
    assert row.derived_mergeability == "blocked-on-validate"


def test_mergeability_blocked_on_ci_failure(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_check_run(head_sha="sha-1", conclusion="failure")
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.ci_conclusion == "failure"
    assert row.derived_mergeability == "blocked-on-ci"


def test_mergeability_blocked_on_conflict(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_pr_conflict(head_sha="sha-1", is_conflicting=True)
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.pr_conflicting is True
    assert row.derived_mergeability == "blocked-on-conflict"


def test_mergeability_mergeable_when_all_green(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_step_envelope(
        task_id, "wf-review", decision="approved", commit_sha="sha-1",
    )
    fixtures.add_step_envelope(
        task_id, "wf-validate", decision="pass", commit_sha="sha-1",
    )
    fixtures.add_check_run(head_sha="sha-1", conclusion="success")
    fixtures.add_pr_conflict(head_sha="sha-1", is_conflicting=False)
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.review_decision == "approved"
    assert row.validate_decision == "pass"
    assert row.ci_conclusion == "success"
    assert row.pr_conflicting is False
    assert row.derived_mergeability == "mergeable"


def test_mergeability_mergeable_when_no_ci_configured(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    """ADR-0013 §"Derived states" #6: NULL CI is treated as "no CI
    configured" and does not block mergeability when review + validate
    are green and there's no conflict."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_step_envelope(
        task_id, "wf-review", decision="approved", commit_sha="sha-1",
    )
    fixtures.add_step_envelope(
        task_id, "wf-validate", decision="pass", commit_sha="sha-1",
    )
    # No check_run_completed at all → ci.conclusion IS NULL.
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.ci_conclusion is None
    assert row.derived_mergeability == "mergeable"


def test_mergeability_invalidates_on_new_head(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    """ADR-0013 §"Per-commit invalidation by construction": land
    approved review + passing validate at SHA X, assert ``mergeable``;
    push ``pr_synchronize`` with new SHA Y; assert ``pending`` (the
    VIEW's filter no longer matches the old SHA, so review / validate
    become NULL until fresh thumbs land at Y)."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-X")
    fixtures.add_step_envelope(
        task_id, "wf-review", decision="approved", commit_sha="sha-X",
    )
    fixtures.add_step_envelope(
        task_id, "wf-validate", decision="pass", commit_sha="sha-X",
    )
    assert _derived(engine, task_id) == "mergeable"

    # Tiny sleep so the synchronize event sorts strictly after the
    # pr_opened event by ``created_at DESC``. ``now()`` returns txn-start
    # time but separate txns differ at the µs level; padding makes the
    # assertion robust under load.
    time.sleep(0.01)
    fixtures.add_pr_synchronize(head_sha="sha-Y", before_sha="sha-X")

    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.head_sha == "sha-Y"
    # The old SHA's thumbs are now invisible to the VIEW.
    assert row.review_decision is None
    assert row.validate_decision is None
    assert row.derived_mergeability == "pending"


def test_mergeability_priority_conflict_wins_over_ci(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    """ADR-0013 priority order: conflict (#2) beats CI (#3)."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_pr_conflict(head_sha="sha-1", is_conflicting=True)
    fixtures.add_check_run(head_sha="sha-1", conclusion="failure")
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.pr_conflicting is True
    assert row.ci_conclusion == "failure"
    assert row.derived_mergeability == "blocked-on-conflict"


def test_mergeability_priority_ci_wins_over_review(
    engine: Engine, fixtures: MergeabilityFixtureBuilder,
) -> None:
    """ADR-0013 priority order: CI failure (#3) beats review
    changes_requested (#4)."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_check_run(head_sha="sha-1", conclusion="failure")
    fixtures.add_step_envelope(
        task_id, "wf-review", decision="changes_requested", commit_sha="sha-1",
    )
    row = _mergeability_row(engine, task_id)
    assert row is not None
    assert row.ci_conclusion == "failure"
    assert row.review_decision == "changes_requested"
    assert row.derived_mergeability == "blocked-on-ci"


# ── Router smoke ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def wait_for_api(api_url: str) -> None:
    """Module-scoped (not autouse): only the router-smoke tests below
    request this fixture. The VIEW-only tests don't depend on the API
    being up — they hit the database directly."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"API not reachable at {api_url}")


def test_get_task_mergeability_endpoint_returns_row(
    engine: Engine,
    fixtures: MergeabilityFixtureBuilder,
    api_url: str,
    wait_for_api: None,
) -> None:
    """``GET /api/v1/tasks/{id}/mergeability`` returns a 200 + the
    full mergeability projection (ADR-0013 §"Surfaces")."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_step_envelope(
        task_id, "wf-review", decision="approved", commit_sha="sha-1",
    )
    fixtures.add_step_envelope(
        task_id, "wf-validate", decision="pass", commit_sha="sha-1",
    )

    resp = httpx.get(
        f"{api_url}/api/v1/tasks/{task_id}/mergeability", timeout=5.0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task_id)
    assert body["repo"] == MergeabilityFixtureBuilder.DEFAULT_REPO
    assert body["pr_number"] == MergeabilityFixtureBuilder.DEFAULT_PR
    assert body["head_sha"] == "sha-1"
    assert body["review_decision"] == "approved"
    assert body["validate_decision"] == "pass"
    assert body["ci_conclusion"] is None
    assert body["pr_conflicting"] is None
    assert body["derived_mergeability"] == "mergeable"


def test_get_task_mergeability_404_on_unknown_task(
    api_url: str, wait_for_api: None,
) -> None:
    resp = httpx.get(
        f"{api_url}/api/v1/tasks/{uuid.uuid4()}/mergeability", timeout=5.0,
    )
    assert resp.status_code == 404


def test_get_task_mergeability_defaults_pending_when_no_pr(
    engine: Engine,
    fixtures: MergeabilityFixtureBuilder,
    api_url: str,
    wait_for_api: None,
) -> None:
    """A task with no PR has no row in the VIEW; the endpoint surfaces
    ``derived_mergeability='pending'`` with every other field NULL."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)

    resp = httpx.get(
        f"{api_url}/api/v1/tasks/{task_id}/mergeability", timeout=5.0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task_id)
    assert body["repo"] is None
    assert body["pr_number"] is None
    assert body["head_sha"] is None
    assert body["derived_mergeability"] == "pending"


def test_get_task_includes_mergeability_field(
    engine: Engine,
    fixtures: MergeabilityFixtureBuilder,
    api_url: str,
    wait_for_api: None,
) -> None:
    """``GET /api/v1/tasks/{id}`` LEFT-JOINs the VIEW and surfaces
    ``mergeability`` on the task response."""
    plan_id = fixtures.make_plan()
    task_id = fixtures.make_task(plan_id)
    fixtures.add_task_pr(task_id)
    fixtures.add_pr_opened(head_sha="sha-1")
    fixtures.add_step_envelope(
        task_id, "wf-review", decision="approved", commit_sha="sha-1",
    )
    fixtures.add_step_envelope(
        task_id, "wf-validate", decision="pass", commit_sha="sha-1",
    )

    resp = httpx.get(f"{api_url}/api/v1/tasks/{task_id}", timeout=5.0)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mergeability"] == "mergeable"
