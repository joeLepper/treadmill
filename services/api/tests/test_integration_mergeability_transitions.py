"""Integration test for mergeability *transitions* (ADR-0013, Phase D.2).

The companion file ``test_integration_task_mergeability.py`` (Phase B.1)
covers every derived state as a discrete fixture case. This file
contributes the *transition* dimension: it drives a single task through
a multi-step lifecycle and asserts the VIEW reflects each transition
correctly.

The lifecycle this file validates end-to-end mirrors ADR-0013
§"Per-commit invalidation by construction":

    1. PR opened at HEAD X            → pending
    2. wf-review approved at X         → pending (validate missing)
    3. wf-validate pass at X           → mergeable (no-CI-configured
                                        treated as green per ADR-0013
                                        §"Derived states" #6)
    4. CI success at X                 → mergeable (redundant
                                        verification — explicit CI
                                        success holds the state)
    5. push pr_synchronize → HEAD Y    → pending (old thumbs invalidated)
    6. fresh thumbs at Y               → mergeable

Plus three supplemental transition cases:

    * ``changes_requested`` at old HEAD does not block new HEAD
    * a new HEAD can REINTRODUCE a blocker (mergeable → blocked-on-ci)
    * the VIEW's commit_sha filter guards against out-of-order thumbs
      that arrive after the push event

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest \\
    services/api/tests/test_integration_mergeability_transitions.py
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine


INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason="set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL (a DEDICATED test database) to run; requires `treadmill-local up`",
)




@pytest.fixture(scope="module")
def database_url() -> str:
    return TEST_DB_URL


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


# Tables truncated between tests. Mirrors the precedent in
# ``test_integration_task_mergeability.py`` — keeping the set in sync
# keeps the two suites composable on the same substrate.
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


# ── Fixture builder ───────────────────────────────────────────────────
#
# Builder shape mirrors ``MergeabilityFixtureBuilder`` in
# ``test_integration_task_mergeability.py`` but adds the higher-level
# ``make_task_with_pr`` + ``seed_*`` helpers D.2 needs to keep each
# transition step a single line of test code. The two builders share no
# code at the seam level (no shared module) because the parent test
# already commits to its naming + because the seeders here phrase the
# action in terms of "what just happened in the world" (``seed_pr_opened``,
# ``seed_check_run_completed``) rather than "what's in the row"
# (``add_pr_opened``, ``add_check_run``). The shape difference is small
# but load-bearing for readability of the lifecycle test.


@pytest.fixture
def fixtures(engine: Engine) -> Iterator["TransitionFixtureBuilder"]:
    builder = TransitionFixtureBuilder(engine)
    builder.truncate_all()
    try:
        yield builder
    finally:
        builder.truncate_all()


class TransitionFixtureBuilder:
    """Seeds the schema for transition tests.

    Difference from ``MergeabilityFixtureBuilder``: this builder thinks
    in lifecycle verbs (``seed_pr_opened``, ``seed_wf_review_completed``,
    ``seed_check_run_completed``) so the transition test reads like the
    narrative in the ADR. Same underlying tables; same envelope shape
    per ADR-0012.
    """

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

    # ── helpers shared with parent fixture ────────────────────────────

    def _make_workflow_version(
        self, conn: Connection, slug: str,
    ) -> uuid.UUID:
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

    def _ensure_role(self, conn: Connection, role_id: str) -> str:
        conn.execute(
            sa.text(
                "INSERT INTO roles (id, model, system_prompt, output_kind) "
                "VALUES (:id, 'claude', '', 'code') ON CONFLICT (id) DO NOTHING"
            ),
            {"id": role_id},
        )
        return role_id

    # ── high-level setup ──────────────────────────────────────────────

    def make_task_with_pr(
        self,
        repo: str,
        pr_number: int,
        title: str = "transition-test task",
        workflow_slug: str = "wf-author",
    ) -> uuid.UUID:
        """Create plan + task + task_prs row in one call.

        The transition test cares about the *task* + its *PR mapping*;
        this helper is the single setup line at the top of each test.
        """
        with self.engine.begin() as conn:
            plan_id = conn.execute(
                sa.text("INSERT INTO plans (repo) VALUES (:r) RETURNING id"),
                {"r": repo},
            ).scalar()
            wv_id = self._make_workflow_version(conn, workflow_slug)
            self._ensure_role(conn, "role-author")
            task_id = conn.execute(
                sa.text(
                    "INSERT INTO tasks "
                    "(plan_id, repo, title, workflow_version_id) "
                    "VALUES (:p, :r, :t, :wv) RETURNING id"
                ),
                {"p": plan_id, "r": repo, "t": title, "wv": wv_id},
            ).scalar()
            conn.execute(
                sa.text(
                    "INSERT INTO task_prs (repo, pr_number, task_id) "
                    "VALUES (:r, :p, :t)"
                ),
                {"r": repo, "p": pr_number, "t": task_id},
            )
        # Cache so callers don't need to pass repo/pr_number on every
        # subsequent seed.
        self._repo_by_task: dict[uuid.UUID, tuple[str, int]] = getattr(
            self, "_repo_by_task", {},
        )
        self._repo_by_task[task_id] = (repo, pr_number)
        return task_id

    def _pr_coords(self, task_id: uuid.UUID) -> tuple[str, int]:
        return self._repo_by_task[task_id]

    # ── lifecycle seeders ─────────────────────────────────────────────

    def seed_pr_opened(self, task_id: uuid.UUID, head_sha: str) -> None:
        repo, pr_number = self._pr_coords(task_id)
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

    def seed_pr_synchronize(
        self,
        task_id: uuid.UUID,
        head_sha: str,
        before_sha: str | None = None,
    ) -> None:
        repo, pr_number = self._pr_coords(task_id)
        payload = {
            "repo": repo,
            "pr_number": pr_number,
            "sender": "tester",
            "head_sha": head_sha,
            "before_sha": before_sha,
        }
        # The VIEW's head LATERAL orders by ``created_at DESC``. Inside
        # a single transaction, ``now()`` is the txn-start time, so two
        # writes in different txns can still resolve to the same µs
        # under heavy load. Pad with a tiny sleep before the new HEAD
        # event to keep ordering deterministic.
        time.sleep(0.01)
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

    def _seed_step_envelope(
        self,
        task_id: uuid.UUID,
        workflow_slug: str,
        decision: str,
        commit_sha: str,
    ) -> uuid.UUID:
        """Insert a completed workflow_run_step with a StepOutput envelope.

        ADR-0012 promotes ``commit_sha`` + ``decision`` to top-level
        envelope fields; the VIEW joins on ``output->>'commit_sha'`` and
        ``output->>'decision'``. Getting this shape wrong is the most
        likely failure mode, so the envelope built here is *exactly*
        ADR-0012's documented shape — six top-level keys.
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
            wv_id = self._make_workflow_version(conn, workflow_slug)
            self._ensure_role(conn, "role-author")
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
                    "VALUES (:r, 0, :n, 'role-author', 'completed', "
                    "CAST(:o AS jsonb), now(), now()) "
                    "RETURNING id"
                ),
                {
                    "r": run_id,
                    "n": workflow_slug,
                    "o": json.dumps(envelope),
                },
            ).scalar()
        return step_id

    def seed_wf_review_completed(
        self, task_id: uuid.UUID, commit_sha: str, decision: str,
    ) -> uuid.UUID:
        return self._seed_step_envelope(
            task_id, "wf-review", decision, commit_sha,
        )

    def seed_wf_validate_completed(
        self, task_id: uuid.UUID, commit_sha: str, decision: str,
    ) -> uuid.UUID:
        return self._seed_step_envelope(
            task_id, "wf-validate", decision, commit_sha,
        )

    def seed_check_run_completed(
        self,
        task_id: uuid.UUID,
        commit_sha: str,
        conclusion: str,
        check_name: str = "ci",
    ) -> None:
        repo, pr_number = self._pr_coords(task_id)
        payload = {
            "repo": repo,
            "pr_number": pr_number,
            "check_name": check_name,
            "conclusion": conclusion,
            "head_sha": commit_sha,
        }
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO events "
                    "(entity_type, action, commit_sha, payload) "
                    "VALUES ('github', 'check_run_completed', :sha, "
                    "CAST(:p AS jsonb))"
                ),
                {"sha": commit_sha, "p": json.dumps(payload)},
            )


# ── helpers ───────────────────────────────────────────────────────────


def _mergeability(engine: Engine, task_id: uuid.UUID) -> str:
    """Return ``derived_mergeability`` for ``task_id``.

    A task with no row in the VIEW (no ``task_prs``) surfaces as
    ``'pending'`` to mirror the endpoint's no-row default. The
    transition tests in this file always have a PR row, so the no-row
    branch is never hit here, but the shape matches the parent file's
    ``_derived`` helper to keep grep-affinity high.
    """
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT derived_mergeability FROM task_mergeability "
                "WHERE task_id = :id"
            ),
            {"id": task_id},
        ).one_or_none()
    return "pending" if row is None else row.derived_mergeability


# ── the required full-lifecycle transition test ───────────────────────


def test_mergeability_transitions_through_full_lifecycle(
    engine: Engine, fixtures: TransitionFixtureBuilder,
) -> None:
    """The VIEW resolves through the full per-commit mergeability
    lifecycle for one task: pending → mergeable (at HEAD X) → pending
    (after push to HEAD Y) → mergeable (at HEAD Y).

    This is the integration test that validates ADR-0013's per-commit
    invalidation contract end-to-end, exercising every transition the
    real world will drive."""
    # 1. Seed task + PR. Initial state: pending (no HEAD yet).
    task_id = fixtures.make_task_with_pr(repo="trans/repo", pr_number=42)
    sha_x = "aaaa" * 10
    fixtures.seed_pr_opened(task_id, head_sha=sha_x)
    assert _mergeability(engine, task_id) == "pending"

    # 2. Seed wf-review approved at sha_x. Still pending — validate has
    #    not run yet, so the mergeable clause's ``validate.decision =
    #    'pass'`` predicate is unsatisfied and the fall-through 'pending'
    #    clause fires.
    fixtures.seed_wf_review_completed(
        task_id, commit_sha=sha_x, decision="approved",
    )
    assert _mergeability(engine, task_id) == "pending"

    # 3. Seed wf-validate pass at sha_x. Now mergeable: per ADR-0013
    #    §"Derived states" #6 the VIEW treats NULL ``ci.conclusion`` as
    #    "no CI configured" (precedent: the per-state test
    #    ``test_mergeability_mergeable_when_no_ci_configured`` in the
    #    sibling B.1 file). Review + validate green + no CI events + no
    #    conflict = mergeable.
    fixtures.seed_wf_validate_completed(
        task_id, commit_sha=sha_x, decision="pass",
    )
    assert _mergeability(engine, task_id) == "mergeable"

    # 4. Seed CI success at sha_x. Still mergeable — CI success holds
    #    the state. This is the redundant-verification step that proves
    #    the VIEW handles a present-and-green CI signal the same as
    #    a no-CI-configured task at this priority slot.
    fixtures.seed_check_run_completed(
        task_id, commit_sha=sha_x, conclusion="success",
    )
    assert _mergeability(engine, task_id) == "mergeable"

    # 5. Push new commit. The VIEW's head LATERAL now returns sha_y;
    #    review / validate joins filter on sha_y and find nothing;
    #    state falls back to pending.
    sha_y = "bbbb" * 10
    fixtures.seed_pr_synchronize(task_id, head_sha=sha_y, before_sha=sha_x)
    assert _mergeability(engine, task_id) == "pending"

    # 6. Fresh thumbs at sha_y. Mergeable again — proof the VIEW
    #    re-resolves cleanly as new commit-keyed signals arrive.
    fixtures.seed_wf_review_completed(
        task_id, commit_sha=sha_y, decision="approved",
    )
    fixtures.seed_wf_validate_completed(
        task_id, commit_sha=sha_y, decision="pass",
    )
    fixtures.seed_check_run_completed(
        task_id, commit_sha=sha_y, conclusion="success",
    )
    assert _mergeability(engine, task_id) == "mergeable"


# ── supplemental transition tests ─────────────────────────────────────


def test_mergeability_changes_requested_at_old_head_does_not_block_new_head(
    engine: Engine, fixtures: TransitionFixtureBuilder,
) -> None:
    """Stale-thumb-clears invariant.

    A ``changes_requested`` review at sha_x is the most-severe blocker
    for sha_x. After a push to sha_y, that thumb is invisible to the
    VIEW (commit_sha filter no longer matches), so derived state falls
    back to ``pending`` (no thumbs at sha_y yet) — not ``blocked-on-review``.
    """
    task_id = fixtures.make_task_with_pr(repo="trans/stale", pr_number=11)
    sha_x = "1111" * 10
    sha_y = "2222" * 10

    fixtures.seed_pr_opened(task_id, head_sha=sha_x)
    fixtures.seed_wf_review_completed(
        task_id, commit_sha=sha_x, decision="changes_requested",
    )
    assert _mergeability(engine, task_id) == "blocked-on-review"

    # Push wipes the stale thumb from the VIEW's view.
    fixtures.seed_pr_synchronize(task_id, head_sha=sha_y, before_sha=sha_x)
    assert _mergeability(engine, task_id) == "pending"


def test_mergeability_ci_failure_at_new_head_blocks_after_previously_mergeable(
    engine: Engine, fixtures: TransitionFixtureBuilder,
) -> None:
    """Inverse of the stale-thumb-clears case: a new push can also
    REINTRODUCE a blocker that wasn't present at the old HEAD.

    Mergeable at sha_x → push to sha_y → CI fails at sha_y →
    ``blocked-on-ci``. The point being made: the VIEW's per-commit
    filter is symmetric — it doesn't only clear blockers, it can show
    fresh ones too."""
    task_id = fixtures.make_task_with_pr(repo="trans/ci-flip", pr_number=22)
    sha_x = "3333" * 10
    sha_y = "4444" * 10

    fixtures.seed_pr_opened(task_id, head_sha=sha_x)
    fixtures.seed_wf_review_completed(
        task_id, commit_sha=sha_x, decision="approved",
    )
    fixtures.seed_wf_validate_completed(
        task_id, commit_sha=sha_x, decision="pass",
    )
    fixtures.seed_check_run_completed(
        task_id, commit_sha=sha_x, conclusion="success",
    )
    assert _mergeability(engine, task_id) == "mergeable"

    fixtures.seed_pr_synchronize(task_id, head_sha=sha_y, before_sha=sha_x)
    # Replicate full review + validate green at the new HEAD so the
    # *only* thing standing between sha_y and mergeable is the CI flip.
    fixtures.seed_wf_review_completed(
        task_id, commit_sha=sha_y, decision="approved",
    )
    fixtures.seed_wf_validate_completed(
        task_id, commit_sha=sha_y, decision="pass",
    )
    fixtures.seed_check_run_completed(
        task_id, commit_sha=sha_y, conclusion="failure",
    )
    assert _mergeability(engine, task_id) == "blocked-on-ci"


def test_mergeability_concurrent_push_does_not_advance_pending_thumbs(
    engine: Engine, fixtures: TransitionFixtureBuilder,
) -> None:
    """Filtering correctness when events arrive out-of-order.

    Imagine the worker for ``wf-review`` at sha_x runs slow. The push
    to sha_y lands first (in event order), then the review row finally
    completes against sha_x. The VIEW *must not* read the sha_x review
    as if it covered sha_y — the commit_sha filter is the guard.

    Sequence:
      1. open PR at sha_x
      2. push to sha_y (HEAD is now sha_y)
      3. wf-review at sha_x lands LATE (after the push)
      4. wf-review at sha_y lands with ``approved``
      5. wf-validate + ci at sha_y land

    Expected: mergeable. The sha_x review never enters the picture
    because the LATERAL join filters on ``output->>'commit_sha' = sha_y``.
    """
    task_id = fixtures.make_task_with_pr(repo="trans/race", pr_number=33)
    sha_x = "5555" * 10
    sha_y = "6666" * 10

    fixtures.seed_pr_opened(task_id, head_sha=sha_x)
    fixtures.seed_pr_synchronize(task_id, head_sha=sha_y, before_sha=sha_x)

    # The slow sha_x review lands after the push. The VIEW's filter
    # must hide it from the sha_y mergeability calculation.
    fixtures.seed_wf_review_completed(
        task_id, commit_sha=sha_x, decision="approved",
    )
    # Pre-condition: at this point the VIEW sees no review/validate/ci
    # at sha_y → pending. The sha_x review is invisible. If the VIEW
    # were buggy and ignored commit_sha, we'd see 'pending' here too
    # (because validate + ci still missing) — so this single assertion
    # isn't proof. The proof is the absence of the sha_x review from
    # the row, asserted below.
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT review_decision, validate_decision, ci_conclusion "
                "FROM task_mergeability WHERE task_id = :id"
            ),
            {"id": task_id},
        ).one()
    assert row.review_decision is None, (
        "sha_x review must not be consulted for sha_y mergeability"
    )
    assert row.validate_decision is None
    assert row.ci_conclusion is None
    assert _mergeability(engine, task_id) == "pending"

    # Now land all three fresh thumbs at sha_y. The sha_x review remains
    # in the events / steps history but never participates in the VIEW.
    fixtures.seed_wf_review_completed(
        task_id, commit_sha=sha_y, decision="approved",
    )
    fixtures.seed_wf_validate_completed(
        task_id, commit_sha=sha_y, decision="pass",
    )
    fixtures.seed_check_run_completed(
        task_id, commit_sha=sha_y, conclusion="success",
    )
    assert _mergeability(engine, task_id) == "mergeable"
