"""``GET /api/v1/dashboard/overview`` — Operator dashboard aggregator.

This endpoint matches ``services/dashboard/src/api/queries.ts``
``useOverview``'s ``queryFn`` shape exactly:

    { accounts, fleet, escalations, tasks, bucketCounts, events }

Field shapes mirror ``services/dashboard/src/api/types.ts``. The mock
layer at ``services/dashboard/src/api/mock.ts`` implements the same
aggregations against in-memory fixtures; this module mirrors them
against the real DB.

Operator bucketing — mirrors ``mock.ts`` ``operatorBucket()`` exactly:

  * ``blocked``  — escalated OR derived_status starts with ``"blocked"``.
  * ``hopper``   — derived_status ∈ {``registered``, ``queued``}.
  * ``inflight`` — everything else (executing / awaiting_review / …).

The bucket math runs server-side; the client gets both the filtered
``tasks`` list and the full ``bucketCounts`` totals (so the bucket pills
on the overview don't drift from the table contents).

Stubs (flagged in the PR description per the task spec, surfaced through
``best_effort`` markers in this file):
  * ``ACCOUNTS`` per-account 24h spend — no token-usage rollup persisted
    yet at the account level, so we approximate from
    ``workflow_run_steps.input/output_tokens`` + a USD estimate via the
    same ratio the mock uses. Falls back to empty when no token data.
  * ``FLEET`` heartbeats — the autoscaler / scheduler heartbeat tables
    aren't in scope for B1, so we stub these from a coarse "any worker
    activity in the last 5m" probe and ``now()`` for ticks. Replace when
    a dedicated heartbeats source lands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session


router = APIRouter()


# ── Response shapes ───────────────────────────────────────────────────────────
# Field names match ``services/dashboard/src/api/types.ts`` 1:1. Pydantic
# enforces the contract at the wire.


class PipelineStep(BaseModel):
    role: str
    status: str


class PullRequest(BaseModel):
    pr_number: int
    branch: str
    head_sha: str
    ci_conclusion: str | None
    review_decision: str | None
    validate_decision: str | None
    pr_conflicting: bool
    derived_mergeability: str


class Task(BaseModel):
    id: str
    title: str
    repo: str
    repo_mode: str
    account: str
    plan_id: str
    derived_status: str
    last_activity: datetime
    started_at: datetime | None
    created_at: datetime
    pipeline: list[PipelineStep]
    workflow: str | None
    pr: PullRequest | None
    escalated: bool
    escalation_reason: str | None = None
    cost_usd: float
    tokens: int


class Event(BaseModel):
    id: str
    entity_type: str
    action: str
    task_id: str | None
    repo: str | None
    created_at: datetime
    detail: str | None = None


class Account(BaseModel):
    name: str
    tokens_24h: int
    usd_est_24h: float


class Fleet(BaseModel):
    workers_running: int
    workers_capacity: int
    autoscaler_last_tick: datetime
    autoscaler_alive_since: datetime
    scheduler_last_tick: datetime
    scheduler_alive_since: datetime


class Escalation(BaseModel):
    task_id: str
    repo: str
    title: str
    escalated_at: datetime
    reason: str | None = None
    # ADR-0062 Step 5: populated only on the closed-incident path
    # (``?include_closed=true``). ``None`` for open incidents (the
    # default surface). When non-null, the value is read from the
    # ``task.escalation_closed`` event payload — the emitter stamps MTTR
    # at close-time (``escalation_close_sweep.emit_escalation_closed``)
    # so consumers don't re-derive it.
    mttr_seconds: int | None = None


class BucketCounts(BaseModel):
    blocked: int
    inflight: int
    hopper: int
    total: int


class OverviewResponse(BaseModel):
    accounts: list[Account]
    fleet: Fleet
    escalations: list[Escalation]
    tasks: list[Task]
    bucketCounts: BucketCounts
    events: list[Event]


# ── Bucket derivation (mirrors mock.ts `operatorBucket()` exactly) ────────────


_HOPPER_STATUSES = frozenset({"registered", "queued"})


def operator_bucket(*, derived_status: str | None, escalated: bool) -> str:
    s = derived_status or ""
    if escalated:
        return "blocked"
    if s.startswith("blocked"):
        return "blocked"
    if s in _HOPPER_STATUSES:
        return "hopper"
    return "inflight"


# Terminal task states — the Overview operator surface filters these
# out (see the WHERE clause in ``_TASKS_SQL``). The merged-PR projection
# emits ``"pr_merged"`` (NOT ``"merged"``); the LIKE pattern in the SQL
# also matches the rare hybrid ``"pr_merged (wf-author: failed)"`` form
# a task can land in when an in-flight workflow run terminates AFTER
# the PR has already auto-merged (the run's outcome no longer matters
# from an operator perspective — the PR is in main).
_TERMINAL_STATUSES = frozenset({"done", "pr_merged", "validated", "cancelled"})


# Map raw event ``entity_type`` values to the dashboard's enum (matches
# ``services/dashboard/src/api/types.ts`` ``Event['entity_type']``).
_EVENT_ENTITY_MAP = {
    "workflow_run": "run",
    "validation": "validate",
}


# Map ``workflow_run_steps.status`` → ``PipelineStep['status']`` (the dashboard
# uses ``"done"`` for completed; everything else passes through).
_STEP_STATUS_MAP = {"completed": "done"}


# Tokens → USD estimate. Treadmill doesn't yet persist a cost-per-task
# rollup, so we approximate at the same ratio the mock surfaces
# (~$1.42 / 96k tokens ≈ $14.79 per 1M tokens — middle-of-the-road
# Sonnet blended in/out). Best-effort; replace when a real rollup lands.
_USD_PER_TOKEN = 14.79 / 1_000_000


# ── SQL: tasks + their joined chrome ──────────────────────────────────────────


_TASKS_SQL = """
SELECT
    t.id::text                       AS id,
    t.title                          AS title,
    t.repo                           AS repo,
    t.plan_id::text                  AS plan_id,
    t.created_at                     AS created_at,
    ts.derived_status                AS derived_status,
    rc.mode                          AS repo_mode,
    rc.claude_account                AS claude_account,
    tp.pr_number                     AS pr_number,
    tp.branch                        AS pr_branch,
    tm.head_sha                      AS pr_head_sha,
    tm.ci_conclusion                 AS pr_ci_conclusion,
    tm.review_decision               AS pr_review_decision,
    tm.validate_decision             AS pr_validate_decision,
    tm.pr_conflicting                AS pr_conflicting,
    tm.derived_mergeability          AS pr_derived_mergeability,
    -- Most-recent workflow run for this task (drives the pipeline strip
    -- + the ``workflow`` field). LATERAL keeps the per-task query tidy.
    latest_run.run_id                AS latest_run_id,
    latest_run.workflow_id           AS latest_workflow_id,
    latest_run.started_at            AS latest_run_started_at,
    -- ``last_activity`` is the most recent event for this task, falling
    -- back to the task's creation time. The dashboard's "age" column
    -- reads it directly.
    COALESCE(last_event.created_at, t.created_at) AS last_activity,
    -- Tokens summed across every llm_call in every execution on this
    -- task (ADR-0087 Option A attribution).
    COALESCE(token_rollup.tokens_total, 0)        AS tokens_total
FROM tasks t
LEFT JOIN task_status      ts ON ts.id      = t.id
LEFT JOIN repo_configs     rc ON rc.repo    = t.repo
LEFT JOIN task_prs         tp ON tp.task_id = t.id
LEFT JOIN task_mergeability tm ON tm.task_id = t.id
LEFT JOIN LATERAL (
    -- ADR-0087: task_executions replaced workflow_runs. Column aliases
    -- preserved so the projection layer is unchanged; the "workflow_id"
    -- slot now carries the dispatched worker_label.
    SELECT te.id AS run_id, te.worker_label AS workflow_id, te.started_at
    FROM task_executions te
    WHERE te.task_id = t.id
    ORDER BY te.started_at DESC
    LIMIT 1
) latest_run ON TRUE
LEFT JOIN LATERAL (
    SELECT MAX(e.created_at) AS created_at
    FROM events e
    WHERE e.task_id = t.id
) last_event ON TRUE
LEFT JOIN LATERAL (
    -- ADR-0087: per-subprocess tokens live in llm_calls, FK to
    -- task_executions (one row per claude --print invocation).
    SELECT SUM(
        COALESCE(lc.input_tokens, 0)
      + COALESCE(lc.output_tokens, 0)
    ) AS tokens_total
    FROM llm_calls lc
    JOIN task_executions te ON te.id = lc.task_execution_id
    WHERE te.task_id = t.id
) token_rollup ON TRUE
WHERE (
        ts.derived_status NOT IN ('done', 'pr_merged', 'validated', 'cancelled')
    AND ts.derived_status NOT LIKE 'pr_merged %'
)
   OR ts.derived_status IS NULL
ORDER BY COALESCE(last_event.created_at, t.created_at) ASC
"""


_PIPELINE_SQL = """
-- ADR-0087: workflow_run_steps is gone. One task_execution = one
-- logical pipeline step; role carries the trigger so the dashboard
-- distinguishes initial work from rework / review passes.
SELECT
    te.id      AS run_id,
    te.trigger AS role,
    te.status  AS status,
    0          AS step_index
FROM task_executions te
WHERE te.id = ANY(:run_ids)
ORDER BY te.id
"""


_ESCALATIONS_SQL = """
-- A task is "escalated" iff its most recent ``task.escalated_to_operator``
-- event has not yet been followed by *either* a
-- ``task.escalation_acknowledged`` (operator dismissed the ribbon) *or*
-- a ``task.escalation_closed`` (ADR-0062 paired close — sweep-detected
-- via ``coordination/escalation_close_sweep.py`` or operator-driven via
-- the CLI). The original mock contract only knew about ack; ADR-0062
-- Step 5 widens the open-set filter so a closed incident also drops out
-- of the operator's "needs attention" list.
WITH last_escalation AS (
    SELECT DISTINCT ON (task_id)
        task_id,
        created_at AS escalated_at,
        payload->>'reason' AS reason
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalated_to_operator'
      AND task_id IS NOT NULL
    ORDER BY task_id, created_at DESC
),
last_ack AS (
    SELECT DISTINCT ON (task_id)
        task_id,
        created_at AS acked_at
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalation_acknowledged'
      AND task_id IS NOT NULL
    ORDER BY task_id, created_at DESC
),
last_close AS (
    SELECT DISTINCT ON (task_id)
        task_id,
        created_at AS closed_at
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalation_closed'
      AND task_id IS NOT NULL
    ORDER BY task_id, created_at DESC
)
SELECT
    le.task_id::text AS task_id,
    t.repo           AS repo,
    t.title          AS title,
    le.escalated_at  AS escalated_at,
    le.reason        AS reason
FROM last_escalation le
JOIN tasks t ON t.id = le.task_id
LEFT JOIN last_ack   la ON la.task_id = le.task_id
LEFT JOIN last_close lc ON lc.task_id = le.task_id
WHERE (la.acked_at IS NULL OR la.acked_at < le.escalated_at)
  AND (lc.closed_at IS NULL OR lc.closed_at < le.escalated_at)
ORDER BY le.escalated_at DESC
"""


# ``?include_closed=true`` ribbon cap. 50 is enough to fill the
# dashboard's "recently closed" strip without paging; if a future surface
# needs more we'll widen this or page properly.
_CLOSED_ESCALATIONS_LIMIT = 50


_CLOSED_ESCALATIONS_SQL = """
-- Recently closed incidents (ADR-0062 Step 5). Pairs each task's most
-- recent ``task.escalation_closed`` with its matching opener so the
-- "recently closed" ribbon can show the original ``escalated_at`` /
-- ``reason`` alongside the MTTR. ``mttr_seconds`` is read straight from
-- the close event's payload — the emitter
-- (``escalation_close_sweep.emit_escalation_closed``) stamps it at
-- close-time so the value reflects the real wall-clock incident
-- duration even for multi-day stalls.
--
-- CTE names are intentionally distinct from ``_ESCALATIONS_SQL`` so the
-- test-suite's SQL-substring dispatch can tell the two queries apart.
WITH closed_event AS (
    SELECT DISTINCT ON (task_id)
        task_id,
        created_at AS closed_at,
        (payload->>'mttr_seconds')::int AS mttr_seconds
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalation_closed'
      AND task_id IS NOT NULL
    ORDER BY task_id, created_at DESC
),
paired_open AS (
    SELECT DISTINCT ON (task_id)
        task_id,
        created_at AS escalated_at,
        payload->>'reason' AS reason
    FROM events
    WHERE entity_type = 'task'
      AND action = 'escalated_to_operator'
      AND task_id IS NOT NULL
    ORDER BY task_id, created_at DESC
)
SELECT
    ce.task_id::text AS task_id,
    t.repo           AS repo,
    t.title          AS title,
    po.escalated_at  AS escalated_at,
    po.reason        AS reason,
    ce.mttr_seconds  AS mttr_seconds
FROM closed_event ce
JOIN paired_open po ON po.task_id = ce.task_id
JOIN tasks t        ON t.id      = ce.task_id
WHERE po.escalated_at <= ce.closed_at
ORDER BY ce.closed_at DESC
LIMIT :limit
"""


_EVENTS_SQL = """
SELECT
    e.id::text          AS id,
    e.entity_type       AS entity_type,
    e.action            AS action,
    e.task_id::text     AS task_id,
    t.repo              AS repo,
    e.created_at        AS created_at,
    e.payload           AS payload
FROM events e
LEFT JOIN tasks t ON t.id = e.task_id
ORDER BY e.created_at DESC
LIMIT :limit
"""


_ACCOUNTS_SQL = """
-- Per-account token rollup for the last 24h (ADR-0087: ported from
-- workflow_run_steps to llm_calls → task_executions → tasks →
-- repo_configs). Accounts with no rows in the window drop out — the
-- operator strip only surfaces accounts currently spending.
SELECT
    COALESCE(rc.claude_account, 'default') AS name,
    COALESCE(SUM(
        COALESCE(lc.input_tokens, 0) + COALESCE(lc.output_tokens, 0)
    ), 0)::bigint AS tokens_24h
FROM llm_calls lc
JOIN task_executions te ON te.id = lc.task_execution_id
JOIN tasks t            ON t.id = te.task_id
LEFT JOIN repo_configs rc ON rc.repo = t.repo
WHERE lc.created_at >= :since
GROUP BY COALESCE(rc.claude_account, 'default')
HAVING COALESCE(SUM(
    COALESCE(lc.input_tokens, 0) + COALESCE(lc.output_tokens, 0)
), 0) > 0
ORDER BY tokens_24h DESC
"""


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get("/overview", response_model=OverviewResponse)
async def get_overview(
    session: Annotated[AsyncSession, Depends(get_session)],
    repo: Annotated[str | None, Query()] = None,
    bucket: Annotated[str | None, Query()] = None,
    account: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    reason: Annotated[
        Literal["architect_cap", "stuck_task_sweep", "gate-broken", "terminal_gate_sweep"] | None,
        Query(),
    ] = None,
    include_closed: Annotated[bool, Query()] = False,
) -> OverviewResponse:
    """Return the operator-dashboard overview payload.

    Query parameters mirror ``OverviewFilters`` on the dashboard:

      * ``repo``    — full ``owner/name`` match.
      * ``bucket``  — one of ``blocked`` / ``inflight`` / ``hopper``.
      * ``account`` — Claude account name (``personal``, ``zephyr``, …).
      * ``q``       — case-insensitive substring across title / id / repo.
      * ``reason``  — escalation sub-classifier from
        ``TaskEscalatedToOperator.reason`` (ADR-0058): one of
        ``architect_cap`` / ``stuck_task_sweep`` / ``gate-broken``.
        Narrows the ``escalations`` array only; ``tasks`` / bucket
        counts stay unfiltered so the page chrome doesn't drift.
      * ``include_closed`` — when truthy (ADR-0062 Step 5), append
        recently-closed incidents to ``escalations`` so the dashboard
        can render a "recently closed" ribbon with MTTR. Closed rows
        carry a non-null ``mttr_seconds`` (open rows always carry
        ``null``), which is how the frontend distinguishes the two
        states without a separate response array. Closed rows do NOT
        feed ``escalation_by_task``, so per-task ``escalated`` flags
        and bucket counts stay aligned with the open-incident set only.

    Filters narrow the ``tasks`` array only — ``bucketCounts`` stays
    global so the bucket-pill totals on the page chrome don't drift when
    a filter is applied.
    """
    raw_tasks = (await session.execute(text(_TASKS_SQL))).mappings().all()

    # Bulk-load the latest execution row per live task in one query
    # to avoid an N+1 against ``task_executions``.
    run_ids = [
        row["latest_run_id"]
        for row in raw_tasks
        if row["latest_run_id"] is not None
    ]
    pipeline_by_run: dict[Any, list[PipelineStep]] = {}
    if run_ids:
        step_rows = (
            await session.execute(text(_PIPELINE_SQL), {"run_ids": run_ids})
        ).mappings().all()
        for srow in step_rows:
            pipeline_by_run.setdefault(srow["run_id"], []).append(
                PipelineStep(
                    role=srow["role"],
                    status=_STEP_STATUS_MAP.get(srow["status"], srow["status"]),
                )
            )

    # Escalations + per-task escalation lookup.
    escalations_rows = (await session.execute(text(_ESCALATIONS_SQL))).mappings().all()
    escalations = [
        Escalation(
            task_id=row["task_id"],
            repo=row["repo"],
            title=row["title"],
            escalated_at=row["escalated_at"],
            reason=row["reason"],
        )
        for row in escalations_rows
    ]
    # ``escalation_by_task`` drives per-task ``escalated`` flags + bucket
    # math. Only OPEN incidents belong here — closed incidents (appended
    # below when ``include_closed`` is set) are informational ribbon
    # rows, not signals that the task currently needs attention.
    escalation_by_task: dict[str, Escalation] = {e.task_id: e for e in escalations}

    # ADR-0062 Step 5: optional "recently closed" ribbon. Appended to
    # the same ``escalations`` array with ``mttr_seconds`` populated;
    # the frontend distinguishes closed from open by the field being
    # non-null. Kept out of ``escalation_by_task`` so bucket counts
    # don't drift when an incident closes.
    if include_closed:
        closed_rows = (
            await session.execute(
                text(_CLOSED_ESCALATIONS_SQL),
                {"limit": _CLOSED_ESCALATIONS_LIMIT},
            )
        ).mappings().all()
        escalations.extend(
            Escalation(
                task_id=row["task_id"],
                repo=row["repo"],
                title=row["title"],
                escalated_at=row["escalated_at"],
                reason=row["reason"],
                mttr_seconds=row["mttr_seconds"],
            )
            for row in closed_rows
        )

    # ``reason`` narrows the surfaced ``escalations`` array (per-reason
    # triage view, ADR-0058 Step 5). Applied AFTER ``escalation_by_task``
    # is built so bucket math + per-task ``escalated`` flags stay
    # independent of the filter — mirrors how ``repo``/``account``
    # narrow ``tasks`` without disturbing ``bucketCounts``. Filters
    # both open and closed rows when ``include_closed`` is set so the
    # ribbon stays consistent with the open-list narrowing.
    if reason is not None:
        escalations = [e for e in escalations if e.reason == reason]

    # Build the full Task list (pre-filter, pre-bucket).
    all_tasks: list[Task] = []
    for row in raw_tasks:
        task_id = row["id"]
        derived_status = row["derived_status"] or "registered"
        escalation = escalation_by_task.get(task_id)
        escalated = escalation is not None
        pipeline = (
            pipeline_by_run.get(row["latest_run_id"], [])
            if row["latest_run_id"] is not None
            else []
        )
        tokens_total = int(row["tokens_total"] or 0)
        pr = None
        if row["pr_number"] is not None:
            pr = PullRequest(
                pr_number=row["pr_number"],
                branch=row["pr_branch"] or "",
                head_sha=(row["pr_head_sha"] or "")[:7],
                ci_conclusion=row["pr_ci_conclusion"],
                review_decision=row["pr_review_decision"],
                validate_decision=row["pr_validate_decision"],
                pr_conflicting=bool(row["pr_conflicting"]),
                derived_mergeability=row["pr_derived_mergeability"] or "pending",
            )
        all_tasks.append(
            Task(
                id=task_id,
                title=row["title"],
                repo=row["repo"],
                repo_mode=row["repo_mode"] or "conform",
                account=row["claude_account"] or "default",
                plan_id=row["plan_id"],
                derived_status=derived_status,
                last_activity=row["last_activity"],
                started_at=row["latest_run_started_at"],
                created_at=row["created_at"],
                pipeline=pipeline,
                workflow=row["latest_workflow_id"],
                pr=pr,
                escalated=escalated,
                escalation_reason=escalation.reason if escalation else None,
                cost_usd=round(tokens_total * _USD_PER_TOKEN, 2),
                tokens=tokens_total,
            )
        )

    # Global bucket counts — computed before filters narrow the list so
    # the page-chrome totals stay stable.
    counts = BucketCounts(blocked=0, inflight=0, hopper=0, total=len(all_tasks))
    for task in all_tasks:
        b = operator_bucket(
            derived_status=task.derived_status,
            escalated=task.escalated,
        )
        if b == "blocked":
            counts.blocked += 1
        elif b == "hopper":
            counts.hopper += 1
        else:
            counts.inflight += 1

    # Apply user filters to the surfaced ``tasks`` array.
    filtered = all_tasks
    if repo is not None:
        filtered = [t for t in filtered if t.repo == repo]
    if account is not None:
        filtered = [t for t in filtered if t.account == account]
    if bucket is not None:
        filtered = [
            t for t in filtered
            if operator_bucket(
                derived_status=t.derived_status, escalated=t.escalated,
            ) == bucket
        ]
    if q:
        ql = q.lower()
        filtered = [
            t for t in filtered
            if ql in t.title.lower()
            or ql in t.id.lower()
            or ql in t.repo.lower()
        ]

    # Events tail — last 30 system events, unfiltered (client filters by
    # entity_type / task_id per the data spec).
    raw_events = (
        await session.execute(text(_EVENTS_SQL), {"limit": 30})
    ).mappings().all()
    events: list[Event] = []
    for row in raw_events:
        payload: dict[str, Any] = row["payload"] or {}
        detail: str | None = None
        if isinstance(payload, dict):
            # ``detail`` is a free-text label; surface a payload string
            # when one's obviously the human-readable form, else None.
            for key in ("detail", "reason", "summary", "message"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    detail = value
                    break
        events.append(
            Event(
                id=row["id"],
                entity_type=_EVENT_ENTITY_MAP.get(
                    row["entity_type"], row["entity_type"],
                ),
                action=row["action"],
                task_id=row["task_id"],
                repo=row["repo"],
                created_at=row["created_at"],
                detail=detail,
            )
        )

    # Accounts strip — token rollup. Stubbed when there's no token data.
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    accounts_rows = (
        await session.execute(text(_ACCOUNTS_SQL), {"since": since})
    ).mappings().all()
    accounts = [
        Account(
            name=row["name"],
            tokens_24h=int(row["tokens_24h"]),
            usd_est_24h=round(int(row["tokens_24h"]) * _USD_PER_TOKEN, 2),
        )
        for row in accounts_rows
    ]

    # Fleet heartbeats — STUB. The autoscaler / scheduler heartbeat
    # surfaces aren't persisted yet; emit ``now()`` for ticks and a
    # zero-worker fleet so the dashboard renders honestly rather than
    # masquerading stale state as live. Replace when heartbeats land.
    now = datetime.now(timezone.utc)
    fleet = Fleet(
        workers_running=0,
        workers_capacity=0,
        autoscaler_last_tick=now,
        autoscaler_alive_since=now,
        scheduler_last_tick=now,
        scheduler_alive_since=now,
    )

    return OverviewResponse(
        accounts=accounts,
        fleet=fleet,
        escalations=escalations,
        tasks=filtered,
        bucketCounts=counts,
        events=events,
    )
