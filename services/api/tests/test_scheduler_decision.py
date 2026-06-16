"""Unit tests for the ADR-0091 scheduler decision (task acb4adcb).

The decision is a PURE function over ``TeamSnapshot`` rows — every
spec case from the plan exercises ``compute_decision`` directly; the
endpoint is a thin wrapper smoke-tested with a stub session.

Spec cases: two teams with pending work → both present, higher priority
first; aging flips a starved team's rank; mid-execute, mid-await-CI, and
mid-await-merge are each NOT quiescent; empty queue → empty list.

Task c31e0994: desired_team (str|None) → desired_teams (list[str]).  The
endpoint now returns the FULL ranked list of teams with pending work;
the daemon applies its own N-cap (tasks 6c2446b2 + bc5cdc23).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.scheduler import (
    AGING_TIME_CONSTANT_MINUTES,
    Decision,
    TeamSnapshot,
    compute_decision,
    is_quiescent,
    router,
)

NOW = datetime(2026, 6, 12, 18, 0, 0, tzinfo=timezone.utc)


def _team(slug: str, **overrides: Any) -> TeamSnapshot:
    base: dict[str, Any] = {
        "slug": slug,
        "pending_tasks": 0,
        "last_served_at": NOW,  # just served → zero aging
        "oldest_pending_plan_at": None,
        "executing": False,
        "open_pr_with_live_task": False,
        "half_registered_pr": False,
    }
    base.update(overrides)
    return TeamSnapshot(**base)


# ── desired_teams: ranked list, priority + aging ─────────────────────


def test_higher_pending_load_wins_between_fresh_teams() -> None:
    a = _team("team-a", pending_tasks=3)
    b = _team("team-b", pending_tasks=1)

    decision = compute_decision([a, b], NOW)

    assert decision.desired_teams[0] == "team-a"
    assert "team-b" in decision.desired_teams
    assert "3 pending task(s)" in decision.reason
    assert "runner-up team-b" in decision.reason


def test_ranked_list_contains_all_pending_teams_highest_first() -> None:
    """desired_teams is the FULL ranked list — all teams with pending work,
    highest score first.  Idle teams are excluded.  The daemon picks its
    own N-cap from the list (tasks 6c2446b2 + bc5cdc23)."""
    a = _team("team-a", pending_tasks=3)
    b = _team("team-b", pending_tasks=1)
    c = _team("team-c", pending_tasks=0)  # idle — no pending work

    decision = compute_decision([a, b, c], NOW)

    assert decision.desired_teams == ["team-a", "team-b"]
    assert "team-c" not in decision.desired_teams


def test_aging_flips_a_long_starved_team() -> None:
    """The fairness term: team-b has LESS pending work but has waited
    long enough (> 2 × AGING_T for the 2-task gap) to outrank team-a.
    Both teams appear in desired_teams; team-b is ranked first."""
    a = _team("team-a", pending_tasks=3, last_served_at=NOW)
    starved_for = timedelta(minutes=AGING_TIME_CONSTANT_MINUTES * 2 + 5)
    b = _team("team-b", pending_tasks=1, last_served_at=NOW - starved_for)

    decision = compute_decision([a, b], NOW)

    assert decision.desired_teams[0] == "team-b"
    assert decision.desired_teams[1] == "team-a"


def test_never_served_team_ages_from_its_oldest_plan() -> None:
    a = _team("team-a", pending_tasks=2, last_served_at=NOW)
    b = _team(
        "team-b",
        pending_tasks=1,
        last_served_at=None,
        oldest_pending_plan_at=NOW - timedelta(hours=2),
    )

    decision = compute_decision([a, b], NOW)

    assert decision.desired_teams[0] == "team-b"  # 1 + 120/30 = 5 > 2
    assert "team-a" in decision.desired_teams


def test_empty_queue_yields_empty_desired_teams() -> None:
    decision = compute_decision(
        [_team("team-a"), _team("team-b")], NOW,
    )
    assert decision.desired_teams == []
    assert decision.reason == "no team has pending work"
    # Quiescence is still reported — the daemon may need to pause idle
    # teams even with nothing to activate.
    assert decision.quiescent_teams == ["team-a", "team-b"]


def test_no_teams_at_all() -> None:
    decision = compute_decision([], NOW)
    assert decision.desired_teams == []
    assert decision.quiescent_teams == []


def test_deterministic_tiebreak_on_equal_scores() -> None:
    """Same score → lexicographic slug, so the daemon never flaps
    between two equal teams on successive polls."""
    a = _team("team-b", pending_tasks=1)
    b = _team("team-a", pending_tasks=1)
    decision = compute_decision([a, b], NOW)
    assert decision.desired_teams[0] == "team-a"
    assert decision.desired_teams[1] == "team-b"


# ── quiescence (ADR-0091 §4; Bert #332 + Carla #342) ─────────────────


def test_mid_execute_is_not_quiescent() -> None:
    team = _team("team-a", executing=True)
    assert not is_quiescent(team)
    assert "team-a" not in compute_decision([team], NOW).quiescent_teams


def test_mid_await_ci_or_merge_is_not_quiescent() -> None:
    """Carla #342: a rework push leaves the worker exited but CI
    running — the open PR + live task predicate covers await-CI and
    await-merge with one conservative test."""
    team = _team("team-a", open_pr_with_live_task=True)
    assert not is_quiescent(team)
    assert "team-a" not in compute_decision([team], NOW).quiescent_teams


def test_half_registered_pr_is_not_quiescent() -> None:
    """Pausing the coordinator mid-POST /task_prs is the orphan-PR
    class — a fresh pr_opened without its bridge row blocks pause."""
    team = _team("team-a", half_registered_pr=True)
    assert not is_quiescent(team)


def test_idle_team_is_quiescent_even_with_pending_work() -> None:
    """Pending-but-idle (between dispatches) is pausable as a FACT; the
    daemon — not this endpoint — excludes the desired team from
    pausing."""
    team = _team("team-a", pending_tasks=4)
    decision = compute_decision([team], NOW)
    assert decision.desired_teams == ["team-a"]
    assert decision.quiescent_teams == ["team-a"]


def test_registered_only_task_does_not_pin_quiescence() -> None:
    """ADR-0091 fix (2026-06-12): a team whose only non-terminal task is
    'registered' (queued, undispatched) MUST be quiescent.

    Root cause: the SQL open_prs CTE used NOT IN ('done', 'cancelled'),
    which includes 'registered'.  A task reset to 'registered' after a
    prior execution may have a stale open task_prs row; that row made
    open_pr_with_live_task=True and pinned the team non-quiescent even
    though no work was in flight.  The fix adds 'registered' to the
    exclusion set so the SQL correctly returns open_pr_with_live_task=False
    for such teams.

    This test pins the pure-function contract: pending_tasks=1 with all
    in-flight flags False → quiescent."""
    team = _team("team-a", pending_tasks=1)  # one registered task, nothing in-flight
    assert is_quiescent(team)
    decision = compute_decision([team], NOW)
    assert "team-a" in decision.quiescent_teams
    assert "team-a" in decision.desired_teams  # still desired — daemon won't pause it


# ── endpoint wrapper smoke ───────────────────────────────────────────


class _StubSession:
    async def execute(self, stmt: Any, params: Any = None):  # noqa: ANN001
        class _Result:
            def all(self) -> list[Any]:
                return [
                    SimpleNamespace(
                        slug="team-a",
                        pending_tasks=2,
                        last_served_at=None,
                        oldest_pending_plan_at=NOW - timedelta(minutes=10),
                        executing=False,
                        open_pr_with_live_task=False,
                        half_registered_pr=False,
                    )
                ]

        return _Result()


def test_endpoint_wraps_pure_decision() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: _StubSession()

    resp = TestClient(app).get("/api/v1/scheduler/decision")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["desired_teams"] == ["team-a"]
    assert body["quiescent_teams"] == ["team-a"]
    assert "formula: pending + wait_min/" in body["reason"]


def test_aging_constant_documents_daemon_dwell_floor() -> None:
    """Carla #342: aging must not out-pace the daemon's anti-flap
    dwell. The constant is the contract surface the daemon task reads —
    pin its floor so a careless lowering trips a test."""
    assert AGING_TIME_CONSTANT_MINUTES >= 15
    assert isinstance(Decision([]), Decision)
