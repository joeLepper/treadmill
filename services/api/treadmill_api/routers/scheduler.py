"""``GET /api/v1/scheduler/decision`` — ADR-0091 team-scheduler brain.

All testable scheduler logic lives HERE (task acb4adcb); the host daemon
is a thin enactor that polls this endpoint and reconciles systemd units
toward it, never duplicating the decision.

Response: ``{desired_teams, desired_team, quiescent_teams, reason}``.

**Transitional dual-field state (expand/contract, tasks c31e0994 → 6c2446b2)**:
Both ``desired_teams`` (new, full ranked list) and ``desired_team`` (back-compat
shim, equals ``desired_teams[0]`` or ``None``) are returned during the A→B
migration window so the still-running single-active daemon can keep reading the
singular field without stalling.  Once task 6c2446b2 ships the daemon migration
(daemon reads ``desired_teams``), a contract-cleanup PR will drop ``desired_team``
from both the dataclass and the response model.  Do NOT remove either field before
that cleanup; alan is tracking the follow-up.

Desired teams — priority + aging (the documented formula)
=========================================================

Plans carry no explicit priority column, so priority is derived from
queue pressure, with aging as the fairness term::

    score(team) = pending_tasks(team) + wait_minutes(team) / AGING_T

* ``pending_tasks`` — count of non-terminal tasks (``derived_status``
  not in ``done``/``cancelled``) across the team's ACTIVE plans. More
  queued work = more claim on the single active slot.
* ``wait_minutes`` — minutes since the team was last SERVED, proxied by
  its most recent ``task_executions.started_at`` (pure read; resets
  naturally every time the team's coordinator dispatches). A team never
  served falls back to its oldest pending plan's ``created_at``.
* ``AGING_T`` (minutes) — how many minutes of waiting buy one
  pending-task's worth of priority. With ``AGING_T = 30``, a team
  starved for 90 minutes outranks a team with 3 more pending tasks: no
  team starves, but bursts of waiting don't thrash the schedule.

INVARIANT (Carla #342 on the plan): ``AGING_T`` MUST be >= the daemon's
anti-flap hysteresis dwell, or aging could demand a swap faster than
the daemon is allowed to perform one. The daemon's dwell default must
not exceed ``AGING_TIME_CONSTANT_MINUTES``; raise this constant if the
dwell grows.

``desired_teams`` is the FULL ranked list of teams with pending work,
highest score first.  Empty list when no team has pending work.  The
daemon applies its own N-cap and subscription-pool logic to this list;
the endpoint never collapses it to a single winner (tasks 6c2446b2 +
bc5cdc23 own the cap/pool side).

Quiescence — safe to pause (ADR-0091 §4, Bert #332 + Carla #342)
================================================================

A team is quiescent ONLY when all three hold:

1. **No task executing** — no ``task_executions`` row with
   ``status='running'`` for the team's worker/evaluator labels.
2. **No in-flight PR** — no OPEN ``task_prs`` row (``closed_at IS
   NULL``) for the team's repo whose task is non-terminal AND not
   ``registered``. This single predicate covers BOTH await-CI and
   await-merge (Carla #342: a rework push leaves the worker exited but
   CI running — the open PR + live task makes the team non-quiescent
   without needing to distinguish the two phases). ``registered`` is
   explicitly excluded: a task reset to ``registered`` after a prior
   execution may carry a stale open ``task_prs`` row; that row must not
   pin the team non-quiescent, since the task has not been dispatched
   and holds no in-flight resources (ADR-0091, 2026-06-12 fix).
3. **No half-registered PR** — no recent ``github.pr_opened`` event
   (last ``HALF_REGISTERED_WINDOW_MINUTES``) for the repo that lacks a
   ``task_prs`` row: the coordinator's POST may be in flight, and
   pausing it mid-registration is the orphan-PR class.

``quiescent_teams`` reports the FACT per team — it may include
``desired_team`` (a desired team between dispatches is momentarily
pausable). The daemon, not this endpoint, decides what to pause
(everything quiescent except the desired team).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, computed_field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session

router = APIRouter(prefix="/api/v1/scheduler", tags=["scheduler"])

AGING_TIME_CONSTANT_MINUTES = 30
"""Minutes of waiting worth one pending task of priority. MUST stay >=
the scheduler daemon's anti-flap dwell (see module docstring)."""

HALF_REGISTERED_WINDOW_MINUTES = 15
"""A pr_opened event younger than this without a task_prs row reads as
a registration in flight — non-quiescent."""

_TERMINAL_TASK_STATUSES = ("done", "cancelled")


# ── Pure decision core ───────────────────────────────────────────────


@dataclass(frozen=True)
class TeamSnapshot:
    """Everything the decision needs about one team, as plain values —
    the endpoint fetches these; tests construct them directly."""

    slug: str
    pending_tasks: int
    last_served_at: datetime | None
    """Most recent task_executions.started_at for the team's labels."""
    oldest_pending_plan_at: datetime | None
    executing: bool
    open_pr_with_live_task: bool
    half_registered_pr: bool


@dataclass(frozen=True)
class Decision:
    desired_teams: list[str]
    quiescent_teams: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def desired_team(self) -> str | None:
        # back-compat shim for the pre-task-B daemon; remove in the
        # contract-cleanup once the daemon reads desired_teams (task 6c2446b2).
        return self.desired_teams[0] if self.desired_teams else None


def _score(team: TeamSnapshot, now: datetime) -> float:
    anchor = team.last_served_at or team.oldest_pending_plan_at or now
    wait_minutes = max(0.0, (now - anchor).total_seconds() / 60.0)
    return team.pending_tasks + wait_minutes / AGING_TIME_CONSTANT_MINUTES


def is_quiescent(team: TeamSnapshot) -> bool:
    return not (
        team.executing
        or team.open_pr_with_live_task
        or team.half_registered_pr
    )


def compute_decision(teams: list[TeamSnapshot], now: datetime) -> Decision:
    """The pure ADR-0091 decision over team snapshots."""
    quiescent = sorted(t.slug for t in teams if is_quiescent(t))
    contenders = [t for t in teams if t.pending_tasks > 0]
    if not contenders:
        return Decision(
            desired_teams=[],
            quiescent_teams=quiescent,
            reason="no team has pending work",
        )
    scored = sorted(
        ((_score(t, now), t) for t in contenders),
        key=lambda pair: (-pair[0], pair[1].slug),
    )
    best_score, best = scored[0]
    runner_up = (
        f"; runner-up {scored[1][1].slug} at {scored[1][0]:.2f}"
        if len(scored) > 1
        else ""
    )
    return Decision(
        desired_teams=[t.slug for _, t in scored],
        quiescent_teams=quiescent,
        reason=(
            f"{best.slug} leads with score {best_score:.2f} "
            f"({best.pending_tasks} pending task(s) + aging"
            f"{runner_up}); formula: pending + wait_min/"
            f"{AGING_TIME_CONSTANT_MINUTES}"
        ),
    )


# ── Row fetching (the thin, untested-logic-free layer) ──────────────


_SNAPSHOT_SQL = """
WITH teams AS (
    SELECT
        tc.repo,
        -- slug = the unit-name key, derived from coordinator_label the
        -- same way `treadmill team up` builds it.
        substring(tc.coordinator_label from '^coordinator-(.*)$') AS slug,
        tc.coordinator_label
    FROM team_configs tc
),
pending AS (
    SELECT p.repo, COUNT(*) AS pending_tasks,
           MIN(p.created_at) AS oldest_plan_at
    FROM plans p
    JOIN plan_status ps ON ps.id = p.id AND ps.derived_status = 'active'
    JOIN tasks t ON t.plan_id = p.id
    JOIN task_status ts ON ts.id = t.id
    -- Terminal set MUST match the dashboard's "still pending" view
    -- (routers/dashboard/overview.py): for PR-backed tasks the terminal
    -- state is `pr_merged` ('done' is only the local-only arm, see
    -- task_executions.py:177). Omitting pr_merged/validated/'pr_merged …'
    -- counted every MERGED task as pending and inflated the ranking score.
    WHERE ts.derived_status NOT IN ('done', 'pr_merged', 'validated', 'cancelled')
      AND ts.derived_status NOT LIKE 'pr_merged %'
    GROUP BY p.repo
),
served AS (
    SELECT teams.repo, MAX(te.started_at) AS last_served_at
    FROM teams
    JOIN task_executions te
      ON te.worker_label LIKE 'worker-' || teams.slug || '-%'
      OR te.worker_label = 'evaluator-' || teams.slug
    GROUP BY teams.repo
),
executing AS (
    SELECT teams.repo, TRUE AS executing
    FROM teams
    JOIN task_executions te
      ON (te.worker_label LIKE 'worker-' || teams.slug || '-%'
          OR te.worker_label = 'evaluator-' || teams.slug)
     AND te.status = 'running'
    GROUP BY teams.repo
),
open_prs AS (
    SELECT tp.repo, TRUE AS open_pr
    FROM task_prs tp
    JOIN task_status ts ON ts.id = tp.task_id
    WHERE tp.closed_at IS NULL
      AND ts.derived_status NOT IN ('done', 'pr_merged', 'validated', 'cancelled', 'registered')
      AND ts.derived_status NOT LIKE 'pr_merged %'
    GROUP BY tp.repo
),
half_registered AS (
    SELECT e.payload->>'repo' AS repo, TRUE AS half_registered
    FROM events e
    WHERE e.entity_type = 'github'
      AND e.action = 'pr_opened'
      AND e.created_at > :half_registered_cutoff
      AND NOT EXISTS (
          SELECT 1 FROM task_prs tp
          WHERE lower(tp.repo) = lower(e.payload->>'repo')
            AND tp.pr_number = (e.payload->>'pr_number')::int
      )
    GROUP BY e.payload->>'repo'
)
SELECT
    teams.slug,
    COALESCE(pending.pending_tasks, 0)       AS pending_tasks,
    served.last_served_at                    AS last_served_at,
    pending.oldest_plan_at                   AS oldest_pending_plan_at,
    COALESCE(executing.executing, FALSE)     AS executing,
    COALESCE(open_prs.open_pr, FALSE)        AS open_pr_with_live_task,
    COALESCE(hr.half_registered, FALSE)      AS half_registered_pr
FROM teams
LEFT JOIN pending   ON pending.repo = teams.repo
LEFT JOIN served    ON served.repo = teams.repo
LEFT JOIN executing ON executing.repo = teams.repo
LEFT JOIN open_prs  ON lower(open_prs.repo) = lower(teams.repo)
LEFT JOIN half_registered hr ON lower(hr.repo) = lower(teams.repo)
"""


async def fetch_team_snapshots(session: AsyncSession) -> list[TeamSnapshot]:
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=HALF_REGISTERED_WINDOW_MINUTES
    )
    rows = (
        await session.execute(
            text(_SNAPSHOT_SQL), {"half_registered_cutoff": cutoff},
        )
    ).all()
    return [
        TeamSnapshot(
            slug=r.slug,
            pending_tasks=int(r.pending_tasks),
            last_served_at=r.last_served_at,
            oldest_pending_plan_at=r.oldest_pending_plan_at,
            executing=bool(r.executing),
            open_pr_with_live_task=bool(r.open_pr_with_live_task),
            half_registered_pr=bool(r.half_registered_pr),
        )
        for r in rows
    ]


# ── Endpoint (thin wrapper) ──────────────────────────────────────────


class SchedulerDecisionResponse(BaseModel):
    desired_teams: list[str]
    quiescent_teams: list[str]
    reason: str

    @computed_field
    @property
    def desired_team(self) -> str | None:
        # back-compat shim for the pre-task-B daemon; remove in the
        # contract-cleanup once the daemon reads desired_teams (task 6c2446b2).
        return self.desired_teams[0] if self.desired_teams else None


@router.get("/decision", response_model=SchedulerDecisionResponse)
async def scheduler_decision(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SchedulerDecisionResponse:
    teams = await fetch_team_snapshots(session)
    decision = compute_decision(teams, now=datetime.now(timezone.utc))
    return SchedulerDecisionResponse(
        desired_teams=decision.desired_teams,
        quiescent_teams=decision.quiescent_teams,
        reason=decision.reason,
    )
