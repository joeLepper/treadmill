"""Integration tests for the conflict-answer staleness rule (task ce42dfed).

Pins migration ``20260612_0100``: a persisted ``github.pr_conflict``
answer is (head, base)-dependent, so the ``task_mergeability`` VIEW
trusts it only when it POSTDATES the repo's latest ``github.pr_merged``
event. Stale answers read as NULL — the state the lazy resolver
(task 536bf319) fires on — so the coordinator's next poll re-derives a
fresh answer at gate time instead of discovering the conflict as a late
merge-time failure.

Run requirements
================

Skipped unless BOTH are set:

  TREADMILL_INTEGRATION=1
  TREADMILL_TEST_DATABASE_URL=postgresql+psycopg://.../<DEDICATED TEST DB>

Unlike the older mergeability integration file, there is deliberately
NO default database URL: this suite truncates ``plans``/``tasks``/
``task_prs``/``events`` between tests, and the events table is the
system of record for live coordination state — pointing it at the live
stack must be an explicit, conscious act. (The older file's
``truncate_all`` also references Phase-5-dropped tables, so it errors
on a current schema; this file seeds only surviving tables.)
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

REPO = "joeLepper/treadmill"
PR = 999
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
def seeded_task(engine: Engine) -> Iterator[uuid.UUID]:
    task_id = uuid.uuid4()
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
                "VALUES (:p, :r, 'staleness test')"
            ),
            {"p": plan_id, "r": REPO},
        )
        conn.execute(
            sa.text(
                "INSERT INTO tasks (id, plan_id, repo, title) "
                "VALUES (:t, :p, :r, 'staleness test task')"
            ),
            {"t": task_id, "p": plan_id, "r": REPO},
        )
        conn.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id) "
                "VALUES (:r, :n, :t)"
            ),
            {"r": REPO, "n": PR, "t": task_id},
        )
    yield task_id
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
    action: str,
    payload: dict,
    commit_sha: str | None,
    created_at: datetime,
) -> None:
    """Insert with an EXPLICIT created_at — the staleness boundary is a
    strict timestamp comparison, so tests must control the clock."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO events "
                "(entity_type, action, commit_sha, payload, created_at) "
                "VALUES ('github', :a, :sha, CAST(:p AS jsonb), :ts)"
            ),
            {
                "a": action,
                "sha": commit_sha,
                "p": json.dumps(payload),
                "ts": created_at,
            },
        )


def _add_head(engine: Engine, head_sha: str, at: datetime) -> None:
    _insert_event(
        engine,
        action="pr_opened",
        payload={
            "repo": REPO,
            "pr_number": PR,
            "sender": "tester",
            "title": "x",
            "head_branch": "feat/x",
            "head_sha": head_sha,
        },
        commit_sha=head_sha,
        created_at=at,
    )


def _add_conflict_answer(
    engine: Engine, head_sha: str, is_conflicting: bool, at: datetime,
) -> None:
    _insert_event(
        engine,
        action="pr_conflict",
        payload={
            "repo": REPO,
            "pr_number": PR,
            "head_sha": head_sha,
            "is_conflicting": is_conflicting,
        },
        commit_sha=head_sha,
        created_at=at,
    )


def _add_merge(
    engine: Engine, at: datetime, repo: str = REPO, pr_number: int = 555,
) -> None:
    _insert_event(
        engine,
        action="pr_merged",
        payload={
            "repo": repo,
            "pr_number": pr_number,
            "sender": "tester",
            "merged_sha": "m" * 40,
            "head_branch": "feat/other",
        },
        commit_sha=None,
        created_at=at,
    )


def _conflict_column(engine: Engine, task_id: uuid.UUID) -> bool | None:
    with engine.connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT pr_conflicting FROM task_mergeability "
                "WHERE task_id = :t"
            ),
            {"t": task_id},
        ).scalar_one()


HEAD = "a" * 40


def test_clean_answer_goes_stale_after_base_movement(
    engine: Engine, seeded_task: uuid.UUID,
) -> None:
    """THE sticky-false bug both #320 reviewers flagged: a persisted
    clean answer followed by any merge in the repo (base moved) must
    read as unresolved, not as silently clean."""
    _add_head(engine, HEAD, _at(0))
    _add_conflict_answer(engine, HEAD, is_conflicting=False, at=_at(1))
    assert _conflict_column(engine, seeded_task) is False  # trusted pre-merge
    _add_merge(engine, at=_at(2))
    assert _conflict_column(engine, seeded_task) is None  # stale → re-check


def test_conflicting_answer_also_goes_stale(
    engine: Engine, seeded_task: uuid.UUID,
) -> None:
    """Base movement can RESOLVE a conflict too — a stale ``true`` must
    also re-derive rather than blocking forever."""
    _add_head(engine, HEAD, _at(0))
    _add_conflict_answer(engine, HEAD, is_conflicting=True, at=_at(1))
    _add_merge(engine, at=_at(2))
    assert _conflict_column(engine, seeded_task) is None


def test_answer_newer_than_latest_merge_is_trusted(
    engine: Engine, seeded_task: uuid.UUID,
) -> None:
    """The boundary: an answer computed AFTER the latest merge stands —
    this is exactly what the resolver writes on the re-check poll."""
    _add_head(engine, HEAD, _at(0))
    _add_merge(engine, at=_at(1))
    _add_conflict_answer(engine, HEAD, is_conflicting=False, at=_at(2))
    assert _conflict_column(engine, seeded_task) is False


def test_no_merges_yet_answer_stands(
    engine: Engine, seeded_task: uuid.UUID,
) -> None:
    """The no-pr_merged-yet edge (COALESCE -infinity): with no base
    movement on record, every answer is trusted."""
    _add_head(engine, HEAD, _at(0))
    _add_conflict_answer(engine, HEAD, is_conflicting=False, at=_at(1))
    assert _conflict_column(engine, seeded_task) is False


def test_other_repo_merge_does_not_invalidate(
    engine: Engine, seeded_task: uuid.UUID,
) -> None:
    """Staleness is per-repo: a merge in a DIFFERENT repo moves nothing
    for this PR's base."""
    _add_head(engine, HEAD, _at(0))
    _add_conflict_answer(engine, HEAD, is_conflicting=False, at=_at(1))
    _add_merge(engine, at=_at(2), repo="MediCoderHQ/medicoder")
    assert _conflict_column(engine, seeded_task) is False


def test_fresh_answer_after_staleness_round_trip(
    engine: Engine, seeded_task: uuid.UUID,
) -> None:
    """Full cycle: clean → merge (stale, NULL) → resolver re-fires and
    persists a fresh dirty answer → blocked-on-conflict at gate time,
    not at merge time."""
    _add_head(engine, HEAD, _at(0))
    _add_conflict_answer(engine, HEAD, is_conflicting=False, at=_at(1))
    _add_merge(engine, at=_at(2))
    assert _conflict_column(engine, seeded_task) is None
    # The lazy resolver's re-check (newer than the merge) wins:
    _add_conflict_answer(engine, HEAD, is_conflicting=True, at=_at(3))
    assert _conflict_column(engine, seeded_task) is True
    with engine.connect() as conn:
        derived = conn.execute(
            sa.text(
                "SELECT derived_mergeability FROM task_mergeability "
                "WHERE task_id = :t"
            ),
            {"t": seeded_task},
        ).scalar_one()
    assert derived == "blocked-on-conflict"
