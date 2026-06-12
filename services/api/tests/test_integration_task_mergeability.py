"""Integration tests for the task_mergeability VIEW — current (post-ADR-0087) shape.

REWRITTEN for task 9200ef54. The original file tested the pre-Phase-5
VIEW through tables ADR-0087 dropped (``roles``, ``workflow_runs``,
``workflow_run_steps``, ``event_triggers``, …): its fixture builder
INSERTed into them and its ``truncate_all`` named them, so on a current
schema every test errored at setup — and its module-scope default
pointed at the LIVE database. Classification of the old coverage:

  * wf-review / wf-validate step-row arms + ADR-0029 severity
    machinery — DEAD: the VIEW's review/validate branches read
    ``task.evaluator_verdict`` / ``review.override`` /
    ``validate.override`` events now (ADR-0087 supersession map).
  * conflict arms — SUPERSEDED by
    ``test_integration_conflict_staleness.py`` (both polarities +
    staleness + the blocked-on-conflict round trip).
  * router smokes — covered by the unit suites
    (``test_routers_tasks_mergeability_resolve.py`` + the tasks-router
    tests); they also defaulted to a LIVE API URL — the same hazard
    class this task removes.
  * review / validate / ci arms + priority + per-commit invalidation
    on the CURRENT view — covered nowhere live. That is what this file
    now tests.

Run requirements (task 9200ef54 safety rule — no live-DB default):

  TREADMILL_INTEGRATION=1
  TREADMILL_TEST_DATABASE_URL=postgresql+psycopg://.../<DEDICATED TEST DB>

Skipped unless BOTH are set. This suite truncates ``plans``/``tasks``/
``task_prs``/``task_dependencies``/``events`` between tests — the events
table is live coordination state, so pointing at the live stack must be
an explicit, conscious act.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine


INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason=(
        "set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL "
        "(a DEDICATED test database — this suite truncates events)"
    ),
)

REPO = "test/merge"
PR = 42
HEAD = "a" * 40
NEW_HEAD = "b" * 40
T0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    services_api_dir = Path(__file__).resolve().parent.parent
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env={**os.environ, "DATABASE_URL": TEST_DB_URL},
        check=True,
    )
    eng = sa.create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def task_id(engine: Engine) -> Iterator[uuid.UUID]:
    tid = uuid.uuid4()
    plan_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE plans, tasks, task_prs, task_dependencies, "
                "events CASCADE"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO plans (id, repo, intent) "
                "VALUES (:p, :r, 'mergeability test')"
            ),
            {"p": plan_id, "r": REPO},
        )
        conn.execute(
            sa.text(
                "INSERT INTO tasks (id, plan_id, repo, title) "
                "VALUES (:t, :p, :r, 'mergeability test task')"
            ),
            {"t": tid, "p": plan_id, "r": REPO},
        )
        conn.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id) "
                "VALUES (:r, :n, :t)"
            ),
            {"r": REPO, "n": PR, "t": tid},
        )
    yield tid
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE plans, tasks, task_prs, task_dependencies, "
                "events CASCADE"
            )
        )


def _insert_event(
    engine: Engine,
    *,
    entity_type: str,
    action: str,
    payload: dict,
    commit_sha: str | None,
    created_at: datetime,
    task_id: uuid.UUID | None = None,
) -> None:
    """Explicit created_at — the review branch resolves by recency and
    the conflict branch by the staleness boundary, so tests own the
    clock."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO events "
                "(entity_type, action, task_id, commit_sha, payload, created_at) "
                "VALUES (:e, :a, :t, :sha, CAST(:p AS jsonb), :ts)"
            ),
            {
                "e": entity_type,
                "a": action,
                "t": task_id,
                "sha": commit_sha,
                "p": json.dumps(payload),
                "ts": created_at,
            },
        )


def _open_pr(engine: Engine, head_sha: str = HEAD, at: datetime | None = None) -> None:
    _insert_event(
        engine,
        entity_type="github",
        action="pr_opened",
        payload={"repo": REPO, "pr_number": PR, "head_sha": head_sha},
        commit_sha=head_sha,
        created_at=at or _at(0),
    )


def _push(engine: Engine, head_sha: str, at: datetime) -> None:
    _insert_event(
        engine,
        entity_type="github",
        action="pr_synchronize",
        payload={"repo": REPO, "pr_number": PR, "head_sha": head_sha},
        commit_sha=head_sha,
        created_at=at,
    )


def _evaluator_verdict(
    engine: Engine, task_id: uuid.UUID, verdict: str, at: datetime,
) -> None:
    _insert_event(
        engine,
        entity_type="task",
        action="evaluator_verdict",
        payload={"verdict": verdict, "pr_number": PR},
        commit_sha=None,
        created_at=at,
        task_id=task_id,
    )


def _review_override(
    engine: Engine, task_id: uuid.UUID, head_sha: str, at: datetime,
) -> None:
    _insert_event(
        engine,
        entity_type="review",
        action="override",
        payload={"commit_sha": head_sha},
        commit_sha=head_sha,
        created_at=at,
        task_id=task_id,
    )


def _validate_override(
    engine: Engine, task_id: uuid.UUID, head_sha: str, at: datetime,
) -> None:
    _insert_event(
        engine,
        entity_type="validate",
        action="override",
        payload={"commit_sha": head_sha},
        commit_sha=head_sha,
        created_at=at,
        task_id=task_id,
    )


def _check_run(
    engine: Engine, head_sha: str, conclusion: str, at: datetime,
) -> None:
    _insert_event(
        engine,
        entity_type="github",
        action="check_run_completed",
        payload={"repo": REPO, "pr_number": PR, "conclusion": conclusion},
        commit_sha=head_sha,
        created_at=at,
    )


def _row(engine: Engine, task_id: uuid.UUID) -> dict:
    with engine.connect() as conn:
        result = conn.execute(
            sa.text("SELECT * FROM task_mergeability WHERE task_id = :t"),
            {"t": task_id},
        ).mappings().one()
        return dict(result)


# ── pending baseline ─────────────────────────────────────────────────


def test_pending_when_pr_opened_but_no_signals(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    _open_pr(engine)
    row = _row(engine, task_id)
    assert row["head_sha"] == HEAD
    assert row["derived_mergeability"] == "pending"


# ── review branch: task.evaluator_verdict (ADR-0087 step 6) ──────────


def test_evaluator_rework_blocks_on_review(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    _open_pr(engine)
    _evaluator_verdict(engine, task_id, "rework", _at(1))
    row = _row(engine, task_id)
    assert row["review_decision"] == "changes_requested"
    assert row["derived_mergeability"] == "blocked-on-review"


def test_evaluator_approve_with_green_ci_is_mergeable(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    _open_pr(engine)
    _check_run(engine, HEAD, "success", _at(1))
    _evaluator_verdict(engine, task_id, "approve", _at(2))
    row = _row(engine, task_id)
    assert row["review_decision"] == "approved"
    assert row["ci_conclusion"] == "success"
    assert row["derived_mergeability"] == "mergeable"


def test_evaluator_verdicts_resolve_by_recency(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    """Rework then approve (the rework loop converging): latest wins."""
    _open_pr(engine)
    _evaluator_verdict(engine, task_id, "rework", _at(1))
    _evaluator_verdict(engine, task_id, "approve", _at(2))
    row = _row(engine, task_id)
    assert row["review_decision"] == "approved"
    assert row["derived_mergeability"] == "mergeable"


def test_review_override_pinned_to_head_approves(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    """The orchestrator's manual review.override is commit_sha-pinned."""
    _open_pr(engine)
    _review_override(engine, task_id, HEAD, _at(1))
    row = _row(engine, task_id)
    assert row["review_decision"] == "approved"
    assert row["derived_mergeability"] == "mergeable"


def test_review_override_for_stale_head_does_not_approve(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    """An override pinned to a superseded head must not approve the new
    head (per-SHA pinning is the override branch's contract)."""
    _open_pr(engine)
    _review_override(engine, task_id, HEAD, _at(1))
    _push(engine, NEW_HEAD, _at(2))
    row = _row(engine, task_id)
    assert row["head_sha"] == NEW_HEAD
    assert row["review_decision"] is None
    assert row["derived_mergeability"] == "pending"


# ── validate branch: validate.override (operator recovery) ──────────


def test_validate_override_reads_pass_at_head(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    _open_pr(engine)
    _validate_override(engine, task_id, HEAD, _at(1))
    _evaluator_verdict(engine, task_id, "approve", _at(2))
    row = _row(engine, task_id)
    assert row["validate_decision"] == "pass"
    assert row["derived_mergeability"] == "mergeable"


# ── ci branch ────────────────────────────────────────────────────────


def test_check_run_failure_blocks_on_ci(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    _open_pr(engine)
    _check_run(engine, HEAD, "failure", _at(1))
    row = _row(engine, task_id)
    assert row["ci_conclusion"] == "failure"
    assert row["derived_mergeability"] == "blocked-on-ci"


def test_ci_failure_outranks_approval(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    """Priority: blocked-on-ci wins over an approved review."""
    _open_pr(engine)
    _check_run(engine, HEAD, "failure", _at(1))
    _evaluator_verdict(engine, task_id, "approve", _at(2))
    row = _row(engine, task_id)
    assert row["review_decision"] == "approved"
    assert row["derived_mergeability"] == "blocked-on-ci"


def test_any_failed_check_blocks_even_with_later_success(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    """The ci LATERAL is EXISTS-based: one failure at HEAD blocks even
    when another check succeeded."""
    _open_pr(engine)
    _check_run(engine, HEAD, "failure", _at(1))
    _check_run(engine, HEAD, "success", _at(2))
    row = _row(engine, task_id)
    assert row["ci_conclusion"] == "failure"
    assert row["derived_mergeability"] == "blocked-on-ci"


# ── per-commit invalidation ──────────────────────────────────────────


def test_new_head_invalidates_sha_pinned_signals(
    engine: Engine, task_id: uuid.UUID,
) -> None:
    """A push moves head_sha: commit-pinned CI signals stop applying.

    Documents a deliberate asymmetry of the current view: evaluator
    verdicts are TASK-scoped, not SHA-pinned, so a standing approval
    survives the push and — with CI unknown at the new head admitted by
    the ``ci IS NULL`` arm — the row still reads ``mergeable``. The
    coordinator's contract is to re-brief the evaluator after any
    post-verdict push (a fresh verdict wins by recency); the VIEW does
    not enforce that re-review by itself.
    """
    _open_pr(engine)
    _check_run(engine, HEAD, "success", _at(1))
    _evaluator_verdict(engine, task_id, "approve", _at(2))
    assert _row(engine, task_id)["derived_mergeability"] == "mergeable"
    _push(engine, NEW_HEAD, _at(3))
    row = _row(engine, task_id)
    assert row["head_sha"] == NEW_HEAD
    assert row["ci_conclusion"] is None  # SHA-pinned: invalidated
    assert row["review_decision"] == "approved"  # task-scoped: survives
    assert row["derived_mergeability"] == "mergeable"
