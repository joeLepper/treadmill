"""Tests for role_eval module (ADR-0056).

Seeds an in-memory SQLite with the minimal subset of the API schema
the retrospective query touches (``tasks`` / ``workflow_runs`` /
``workflow_run_steps`` / ``events``), inserts three fake tasks with
distinct outcome profiles, and asserts the per-task dicts + the
aggregate score match values computed by hand from ADR-0056's
``clean_fraction - 0.5 * looped_fraction`` rule.

SQLite stands in for Postgres here because the production query only
uses SQL standard features (``LIKE``, ``SUM(CASE WHEN ...)``,
``EXISTS``, ``GROUP BY``) — no JSONB, no PG arrays, no ``FILTER``
clause. The test pattern keeps the SQL in the hot path under
inspection without dragging in a live database for every run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from treadmill_agent.role_eval import RetroEvalResult, evaluate_role_retrospectively


ROLE = "role-code-author"


def _setup_engine() -> sa.Engine:
    """Build an in-memory SQLite with the minimal table set the
    retrospective query reads. Column types match the API's PG schema
    only as far as the query needs (string IDs, ints for tokens, a
    timestamp for ``completed_at``)."""
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY)"
        ))
        conn.execute(sa.text(
            "CREATE TABLE workflow_runs ("
            " id TEXT PRIMARY KEY,"
            " task_id TEXT NOT NULL,"
            " trigger TEXT NOT NULL"
            ")"
        ))
        conn.execute(sa.text(
            "CREATE TABLE workflow_run_steps ("
            " id TEXT PRIMARY KEY,"
            " run_id TEXT NOT NULL,"
            " role_id TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " input_tokens INTEGER,"
            " output_tokens INTEGER,"
            " completed_at TIMESTAMP"
            ")"
        ))
        conn.execute(sa.text(
            "CREATE TABLE events ("
            " id TEXT PRIMARY KEY,"
            " task_id TEXT,"
            " action TEXT NOT NULL"
            ")"
        ))
    return engine


def _new_id() -> str:
    return str(uuid.uuid4())


def _insert_task(conn: sa.Connection, task_id: str) -> None:
    conn.execute(sa.text("INSERT INTO tasks (id) VALUES (:id)"), {"id": task_id})


def _insert_run(
    conn: sa.Connection, *, task_id: str, trigger: str,
) -> str:
    run_id = _new_id()
    conn.execute(
        sa.text(
            "INSERT INTO workflow_runs (id, task_id, trigger)"
            " VALUES (:id, :task_id, :trigger)"
        ),
        {"id": run_id, "task_id": task_id, "trigger": trigger},
    )
    return run_id


def _insert_step(
    conn: sa.Connection,
    *,
    run_id: str,
    role_id: str,
    completed_at: datetime,
    input_tokens: int,
    output_tokens: int,
    status: str = "completed",
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO workflow_run_steps"
            " (id, run_id, role_id, status, input_tokens, output_tokens,"
            "  completed_at)"
            " VALUES (:id, :run_id, :role_id, :status, :in_t, :out_t, :ts)"
        ),
        {
            "id": _new_id(),
            "run_id": run_id,
            "role_id": role_id,
            "status": status,
            "in_t": input_tokens,
            "out_t": output_tokens,
            "ts": completed_at,
        },
    )


def _insert_event(conn: sa.Connection, *, task_id: str, action: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO events (id, task_id, action) VALUES (:id, :tid, :a)"
        ),
        {"id": _new_id(), "tid": task_id, "a": action},
    )


@pytest.fixture
def seeded() -> tuple[sa.Engine, dict[str, str]]:
    """Seed three tasks with distinct outcome profiles + a fourth out-
    of-window task that the query should NOT pick up.

    Tasks:
      A — clean   : 2 role steps (registered trigger), pr_merged.
                    runs=2, feedback_runs=0, merged=True → clean.
      B — looped  : 4 role steps incl. 2 ``self:wf-feedback-*``, no merge.
                    runs=4, feedback_runs=2, merged=False → looped only.
      C — mixed   : 3 role steps incl. 1 wf-feedback + 1 architect-amend,
                    pr_merged. runs=3, feedback_runs=1, amend_runs=1,
                    merged=True → BOTH clean (merged+≤3) AND looped (≥1 fb).
      D — out-of-window: 5 role steps but ``completed_at`` is 2 days old.
                    Filtered out by the cutoff; the query must not see it.
    """
    engine = _setup_engine()
    now = datetime.now(tz=timezone.utc)
    recent = now - timedelta(hours=1)
    stale = now - timedelta(days=2)

    ids = {
        "A": _new_id(),
        "B": _new_id(),
        "C": _new_id(),
        "D": _new_id(),
    }

    with engine.begin() as conn:
        for tid in ids.values():
            _insert_task(conn, tid)

        # Task A: 2 clean runs.
        for in_t, out_t in ((50, 50), (60, 140)):
            run = _insert_run(conn, task_id=ids["A"], trigger="registered")
            _insert_step(
                conn, run_id=run, role_id=ROLE, completed_at=recent,
                input_tokens=in_t, output_tokens=out_t,
            )
        _insert_event(conn, task_id=ids["A"], action="pr_merged")

        # Task B: 4 runs including 2 wf-feedback triggers, no merge.
        triggers_b = [
            "registered",
            "self:wf-feedback-validation-fail",
            "registered",
            "self:wf-feedback-review-fail",
        ]
        for trig, in_t, out_t in zip(triggers_b, (40, 80, 60, 70), (60, 120, 40, 30)):
            run = _insert_run(conn, task_id=ids["B"], trigger=trig)
            _insert_step(
                conn, run_id=run, role_id=ROLE, completed_at=recent,
                input_tokens=in_t, output_tokens=out_t,
            )

        # Task C: 3 runs — 1 wf-feedback, 1 architect-amend; merged.
        triggers_c = [
            "registered",
            "self:wf-feedback-validation-fail",
            "self:architect-amend",
        ]
        for trig, in_t, out_t in zip(triggers_c, (100, 150, 80), (100, 90, 80)):
            run = _insert_run(conn, task_id=ids["C"], trigger=trig)
            _insert_step(
                conn, run_id=run, role_id=ROLE, completed_at=recent,
                input_tokens=in_t, output_tokens=out_t,
            )
        _insert_event(conn, task_id=ids["C"], action="pr_merged")

        # Task D: outside the window; would be looped if counted.
        for _ in range(5):
            run = _insert_run(
                conn, task_id=ids["D"],
                trigger="self:wf-feedback-validation-fail",
            )
            _insert_step(
                conn, run_id=run, role_id=ROLE, completed_at=stale,
                input_tokens=10, output_tokens=10,
            )

        # Also insert a completed step for a DIFFERENT role on Task A
        # to verify the role_id filter is honored (this row must not
        # be counted in the role-code-author aggregation).
        other_run = _insert_run(
            conn, task_id=ids["A"], trigger="registered",
        )
        _insert_step(
            conn, run_id=other_run, role_id="role-reviewer",
            completed_at=recent, input_tokens=999, output_tokens=999,
        )

    return engine, ids


def test_three_tasks_score_and_per_task_match_hand_computed(
    seeded: tuple[sa.Engine, dict[str, str]],
) -> None:
    engine, ids = seeded

    with Session(engine) as session:
        result = evaluate_role_retrospectively(
            ROLE, window_seconds=3600 * 6, session=session,
        )

    assert isinstance(result, RetroEvalResult)
    # Three tasks touched in-window; the 2-day-old task D is filtered.
    assert result.n == 3

    by_id = {row["task_id"]: row for row in result.per_task}
    assert set(by_id.keys()) == {ids["A"], ids["B"], ids["C"]}

    # Task A — clean: 2 role steps, no special triggers, merged.
    a = by_id[ids["A"]]
    assert a["runs"] == 2
    assert a["feedback_runs"] == 0
    assert a["amend_runs"] == 0
    assert a["merged"] is True
    # tokens = (50+50) + (60+140) = 300. The cross-role step (role-reviewer)
    # MUST NOT contribute — its 999+999 would blow the total past 300.
    assert a["tokens"] == 300

    # Task B — looped: 4 role steps, 2 wf-feedback, no merge.
    b = by_id[ids["B"]]
    assert b["runs"] == 4
    assert b["feedback_runs"] == 2
    assert b["amend_runs"] == 0
    assert b["merged"] is False
    # tokens = 100 + 200 + 100 + 100 = 500
    assert b["tokens"] == 500

    # Task C — mixed: 3 role steps, 1 wf-feedback + 1 architect-amend, merged.
    c = by_id[ids["C"]]
    assert c["runs"] == 3
    assert c["feedback_runs"] == 1
    assert c["amend_runs"] == 1
    assert c["merged"] is True
    # tokens = 200 + 240 + 160 = 600
    assert c["tokens"] == 600

    # Hand-computed aggregate:
    #   clean (merged AND runs<=3): A (2 runs + merged), C (3 runs + merged)
    #     → clean_count = 2
    #   looped (feedback_runs >= 1): B (2 fb), C (1 fb) → looped_count = 2
    #   n = 3
    #   score = 2/3 - 0.5 * 2/3 = 2/3 - 1/3 = 1/3
    assert result.score == pytest.approx(1.0 / 3.0)


def test_empty_population_returns_zero_score() -> None:
    """No completed steps in the window → score 0.0, n 0, no per-task rows."""
    engine = _setup_engine()
    with Session(engine) as session:
        result = evaluate_role_retrospectively(
            ROLE, window_seconds=3600, session=session,
        )

    assert result.n == 0
    assert result.score == 0.0
    assert result.per_task == []


def test_role_id_filter_excludes_other_roles() -> None:
    """A completed step for a DIFFERENT role on a task must not show up
    in the target role's aggregation, even when the task is in-window."""
    engine = _setup_engine()
    now = datetime.now(tz=timezone.utc)
    task_id = _new_id()
    with engine.begin() as conn:
        _insert_task(conn, task_id)
        run = _insert_run(conn, task_id=task_id, trigger="registered")
        _insert_step(
            conn, run_id=run, role_id="role-reviewer",
            completed_at=now - timedelta(minutes=5),
            input_tokens=10, output_tokens=20,
        )
        _insert_event(conn, task_id=task_id, action="pr_merged")

    with Session(engine) as session:
        result = evaluate_role_retrospectively(
            ROLE, window_seconds=3600, session=session,
        )

    assert result.n == 0
    assert result.per_task == []


def test_only_completed_steps_are_counted() -> None:
    """A ``running`` / ``failed`` step for the target role within the
    window must NOT contribute. Only ``status='completed'`` counts."""
    engine = _setup_engine()
    now = datetime.now(tz=timezone.utc)
    task_id = _new_id()
    with engine.begin() as conn:
        _insert_task(conn, task_id)
        # A failed step — should be excluded.
        run_failed = _insert_run(
            conn, task_id=task_id, trigger="registered",
        )
        _insert_step(
            conn, run_id=run_failed, role_id=ROLE,
            completed_at=now - timedelta(minutes=10),
            input_tokens=10, output_tokens=10, status="failed",
        )
        # A completed step — should count.
        run_ok = _insert_run(conn, task_id=task_id, trigger="registered")
        _insert_step(
            conn, run_id=run_ok, role_id=ROLE,
            completed_at=now - timedelta(minutes=5),
            input_tokens=20, output_tokens=30,
        )
        _insert_event(conn, task_id=task_id, action="pr_merged")

    with Session(engine) as session:
        result = evaluate_role_retrospectively(
            ROLE, window_seconds=3600, session=session,
        )

    assert result.n == 1
    only = result.per_task[0]
    assert only["runs"] == 1
    assert only["tokens"] == 50
    assert only["merged"] is True
    # Single clean task, no loops → score = 1.0
    assert result.score == pytest.approx(1.0)
