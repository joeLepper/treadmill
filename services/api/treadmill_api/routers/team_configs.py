"""``/api/v1/team_configs`` + ``/api/v1/queue_depth`` — coordinator/worker
label registry per repo (Task C of the combined ADR-0085+0086 plan).

Four CRUD endpoints on ``team_configs`` (POST upsert / GET list / GET by
repo / DELETE) plus one query endpoint (``GET /api/v1/queue_depth``).

``queue_depth`` excludes tasks where ``tasks.created_by`` matches a
``coordinator_label`` registered in ``team_configs`` — coordinators
register their own brief-emitted tasks, which the operator-facing depth
shouldn't double-count. The query reads from the ``task_status`` view
(columns: ``id``, ``derived_status`` — NOT ``task_id`` / ``status``;
``tasks`` itself has no status column).

The ``{repo:path}`` path-converter on the per-repo routes lets repos
with slashes (``owner/name``) round-trip cleanly without URL-encoding
gymnastics.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.team_config_store import TeamConfigStore


router = APIRouter(prefix="/api/v1", tags=["team_configs"])

_store = TeamConfigStore()


Lifecycle = Literal["ephemeral", "persistent", "manual"]
MergeTarget = Literal["feature-branch", "main"]


class TeamConfigRow(BaseModel):
    """Wire representation of one ``team_configs`` row."""

    id: uuid.UUID
    repo: str
    coordinator_label: str
    evaluator_label: str | None
    worker_labels: list[str]
    lifecycle: Lifecycle
    merge_target: MergeTarget
    created_at: datetime
    updated_at: datetime


class TeamConfigUpsert(BaseModel):
    repo: str = Field(min_length=1, max_length=255)
    coordinator_label: str = Field(min_length=1, max_length=64)
    evaluator_label: str | None = Field(default=None, max_length=64)
    worker_labels: list[str] = Field(default_factory=list)
    # None → INSERT uses the column server-default; UPDATE preserves the existing
    # value (a plain re-`team up` never silently resets a repo's mode).
    lifecycle: Lifecycle | None = Field(default=None)
    merge_target: MergeTarget | None = Field(default=None)


class TeamConfigClaim(BaseModel):
    """Body of the atomic standup-lease claim (ADR-0109/0110 step 4). ``repo`` is
    the path; these are the labels/modes to write iff this caller wins the lease."""

    coordinator_label: str = Field(min_length=1, max_length=64)
    evaluator_label: str | None = Field(default=None, max_length=64)
    worker_labels: list[str] = Field(default_factory=list)
    lifecycle: Lifecycle | None = Field(default=None)
    merge_target: MergeTarget | None = Field(default=None)


class TeamConfigClaimResult(BaseModel):
    config: TeamConfigRow
    claimed: bool
    """True → this caller WON the standup lease and must perform the host-side
    side-effects (render templates + start systemd). False → a team was already
    standing; ATTACH to ``config`` (the standing team), do NOT stand up a second."""


class QueueDepth(BaseModel):
    visible: int
    in_flight: int


@router.post(
    "/team_configs",
    response_model=TeamConfigRow,
    status_code=status.HTTP_200_OK,
)
async def upsert_team_config(
    body: TeamConfigUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    force: bool = False,
) -> TeamConfigRow:
    """Upsert a ``team_configs`` row.

    ADR-0087 scale-down guard: if the upsert SHRINKS ``worker_labels``
    relative to the current persisted row, refuse with 409 when any
    running ``task_executions`` row references a to-be-removed
    ``worker_label`` — the in-flight work would be orphaned by the
    re-spawn loop. ``?force=true`` skips the check (operator's
    explicit acknowledgement that in-flight work is being abandoned).

    The check uses ``to_regclass('task_executions')`` so it is safe to
    run before PR-C (Wave 2) creates the table — when the table does
    not yet exist, the guard is structurally inert.
    """
    current = await _store.get_by_repo(session, body.repo)
    if current is not None and not force:
        removed_labels = set(current.worker_labels) - set(body.worker_labels)
        if removed_labels:
            in_flight = await _in_flight_task_executions_for_labels(
                session, removed_labels
            )
            if in_flight:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "scale-down would orphan in-flight task_executions on "
                        f"worker labels {sorted(removed_labels)!r}: "
                        f"{in_flight!r}. Wait for these task_executions to "
                        "reach status='completed' or 'failed', or re-run "
                        "with ?force=true to override."
                    ),
                )

    row = await _store.upsert(
        session,
        repo=body.repo,
        coordinator_label=body.coordinator_label,
        worker_labels=body.worker_labels,
        evaluator_label=body.evaluator_label,
        lifecycle=body.lifecycle,
        merge_target=body.merge_target,
    )
    await session.commit()
    return TeamConfigRow.model_validate(row, from_attributes=True)


@router.post(
    "/team_configs/{repo:path}/claim",
    response_model=TeamConfigClaimResult,
    status_code=status.HTTP_200_OK,
)
async def claim_team_config(
    repo: str,
    body: TeamConfigClaim,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TeamConfigClaimResult:
    """Atomic per-repo STANDUP-LEASE claim (ADR-0109/0110 step 4).

    The watcher calls this on ``plan.submitted`` before standing a team up. Under
    two concurrent claims for one repo, EXACTLY ONE gets ``claimed=True`` (it must
    do the host-side standup); the other gets ``claimed=False`` and the standing
    team's config (attach). ``INSERT ... ON CONFLICT DO NOTHING`` — a conflict does
    NOT mutate the standing team. Runs in the request's default (READ COMMITTED)
    transaction, which the loser's post-insert read requires.
    """
    row, claimed = await _store.claim(
        session,
        repo=repo,
        coordinator_label=body.coordinator_label,
        worker_labels=body.worker_labels,
        evaluator_label=body.evaluator_label,
        lifecycle=body.lifecycle,
        merge_target=body.merge_target,
    )
    await session.commit()
    return TeamConfigClaimResult(
        config=TeamConfigRow.model_validate(row, from_attributes=True),
        claimed=claimed,
    )


async def _in_flight_task_executions_for_labels(
    session: AsyncSession, worker_labels: set[str]
) -> list[str]:
    """Return task_execution IDs whose worker_label is in
    ``worker_labels`` and whose status is ``running``.

    Returns an empty list when the ``task_executions`` table does not
    exist yet (Phase 3 — PR-C — has not landed). ``to_regclass`` is
    the canonical Postgres existence check for a relation by name; it
    returns ``NULL`` for missing relations without raising.
    """
    if not worker_labels:
        return []
    exists = await session.execute(
        text("SELECT to_regclass('task_executions')")
    )
    if exists.scalar_one_or_none() is None:
        return []
    result = await session.execute(
        text(
            """
            SELECT id::text
            FROM task_executions
            WHERE worker_label = ANY(:labels)
              AND status = 'running'
            ORDER BY started_at
            """
        ),
        {"labels": list(worker_labels)},
    )
    return [row[0] for row in result.fetchall()]


class DrainItem(BaseModel):
    task_id: str
    derived_status: str | None
    reason: str  # "team_active" | "post_merge_unsettled"


class DrainStatus(BaseModel):
    """Whether a team is safe to tear down (ADR-0109 drain-guard).

    ``clean`` is True only when NO task is team-active and no merged task has an
    unsettled deploy. ``parked`` (escalated tasks) does NOT block teardown — it is
    parked-on-human and re-stands-up on the operator's response (ADR-0109 amendment).
    """

    repo: str
    clean: bool
    blocking: list[DrainItem]  # team-active OR merged-but-deploy-unsettled → BLOCK
    parked: list[str]          # escalated task ids → allow-with-tracking (do NOT block)
    last_activity_at: datetime | None = None
    """Most recent activity for this team — the max event time across the team's
    tasks (fallback: the newest team task's creation). NULL when the team has no
    tasks. The idle-sweep applies an idle-grace against this; a clean team whose
    last activity is recent is NOT swept, to avoid thrash right after a plan ends."""


# Terminal-good derived_status set — the exact predicate the dashboard uses
# (routers/dashboard/overview.py): a task is terminal iff derived_status is one of
# these or begins with "pr_merged " (per-worker prefixed).
_TERMINAL_STATUSES = ("done", "pr_merged", "validated", "cancelled")

# One row per task of the team (tasks.created_by = coordinator_label), carrying its
# derived_status and whether it currently has an OPEN operator escalation (the exact
# open-escalation logic from overview.py: latest escalated_to_operator not yet followed
# by an ack or a close).
_TEAM_DRAIN_SQL = text(
    """
    WITH team_tasks AS (
        SELECT id, plan_id FROM tasks WHERE created_by = :coordinator_label
    ),
    last_escalation AS (
        SELECT DISTINCT ON (task_id) task_id, created_at AS escalated_at
        FROM events
        WHERE entity_type = 'task' AND action = 'escalated_to_operator'
          AND task_id IS NOT NULL
        ORDER BY task_id, created_at DESC
    ),
    last_ack AS (
        SELECT DISTINCT ON (task_id) task_id, created_at AS acked_at
        FROM events
        WHERE entity_type = 'task' AND action = 'escalation_acknowledged'
          AND task_id IS NOT NULL
        ORDER BY task_id, created_at DESC
    ),
    last_close AS (
        SELECT DISTINCT ON (task_id) task_id, created_at AS closed_at
        FROM events
        WHERE entity_type = 'task' AND action = 'escalation_closed'
          AND task_id IS NOT NULL
        ORDER BY task_id, created_at DESC
    )
    SELECT
        tt.id::text AS task_id,
        tt.plan_id::text AS plan_id,
        ts.derived_status AS derived_status,
        (le.task_id IS NOT NULL
         AND (la.acked_at IS NULL OR la.acked_at < le.escalated_at)
         AND (lc.closed_at IS NULL OR lc.closed_at < le.escalated_at)) AS escalated
    FROM team_tasks tt
    LEFT JOIN task_status  ts ON ts.id = tt.id
    LEFT JOIN last_escalation le ON le.task_id = tt.id
    LEFT JOIN last_ack     la ON la.task_id = tt.id
    LEFT JOIN last_close   lc ON lc.task_id = tt.id
    """
)


def _is_terminal(derived_status: str | None) -> bool:
    s = derived_status or ""
    return s in _TERMINAL_STATUSES or s.startswith("pr_merged ")


def _is_merged(derived_status: str | None) -> bool:
    s = derived_status or ""
    return s == "pr_merged" or s.startswith("pr_merged ")


def _classify(derived_status: str | None, escalated: bool) -> str:
    """Pure per-state drain classification (fail-closed). Returns:
    - ``parked``  — escalated: parked-on-human, does NOT block teardown (tracked).
    - ``merged``  — a terminal pr_merged task: allowed UNLESS its deploy is unsettled
      (the caller applies the post-merge deploy check on top).
    - ``clean``   — other terminal (done/validated/cancelled): allows teardown.
    - ``block``   — anything else (non-terminal / unknown): TEAM-ACTIVE, blocks.
    Fail-closed: an unrecognized derived_status is not terminal → ``block``.
    """
    if escalated:
        return "parked"
    if _is_merged(derived_status):
        return "merged"
    if _is_terminal(derived_status):
        return "clean"
    return "block"


async def _merge_shas_for_tasks(
    session: AsyncSession, task_ids: list[str]
) -> dict[str, str]:
    """Merge commit_sha per pr_merged task (from its ``github.pr_merged`` event).

    The canonical merge signal is ``entity_type='github', action='pr_merged'`` —
    the SAME event the task_status view, the escalation close-sweep, and
    task_executions all key on. No ``task``-entity ``pr_merged`` event exists; a
    filter on ``entity_type='task'`` would return zero rows and silently disable
    the post-merge deploy check. ``events.commit_sha`` is populated for this event
    with the merge commit sha (ADR-0014 commit-anchor extraction); ``task_id`` is
    FK-resolved from ``task_prs`` on ingress.
    """
    if not task_ids or await _relation_missing(session, "events"):
        return {}
    result = await session.execute(
        text(
            """
            SELECT DISTINCT ON (task_id) task_id::text, commit_sha
            FROM events
            WHERE entity_type = 'github' AND action = 'pr_merged'
              AND task_id = ANY(:ids) AND commit_sha IS NOT NULL
            ORDER BY task_id, created_at DESC
            """
        ),
        {"ids": task_ids},
    )
    return {row[0]: row[1] for row in result.fetchall()}


async def _unsettled_deploy_shas(
    session: AsyncSession, shas: list[str]
) -> set[str]:
    """Of ``shas``, those whose post-merge observation is NOT settled — the coordinator
    must stay to OBSERVE + escalate on failure (ADR-0087 §3.7).

    The two streams are DISTINCT and must be settled PER stream (Ernie): ``deploy`` emits
    started/succeeded/failed; ``staging_smoke`` emits passed/failed and has NO 'started'
    marker. A sha is UNSETTLED when EITHER:
      - a deploy STARTED but reached no deploy terminal (succeeded/failed), OR
      - a deploy SUCCEEDED but its staging_smoke has not reached a terminal (passed/failed).
    The second clause is the smoke-pending rule: because staging_smoke has no 'started'
    signal, a successful deploy is treated as implying a pending smoke until the smoke
    terminal arrives — otherwise "deploy succeeded, smoke coming" would read as settled
    and a later staging_smoke.failed would have no coordinator to escalate it.
    ``deploy.failed`` is settled (the failure IS the terminal; it surfaces as an
    escalation, handled by the parked-on-human path). A sha with no deploy events (a
    feature-branch merge that never reached main) is settled.

    ASSUMPTION (main-mode only; --force escapes): a successful staging deploy is followed
    by a staging smoke. A main-mode repo that never smokes would over-block here; that is
    fail-closed and rare (feature-branch is the default and never deploys). A
    ``staging_smoke.started`` event, if added later, would let us drop the implication.
    """
    if not shas or await _relation_missing(session, "events"):
        return set()
    result = await session.execute(
        text(
            """
            SELECT commit_sha
            FROM events
            WHERE commit_sha = ANY(:shas)
              AND entity_type IN ('deploy', 'staging_smoke')
            GROUP BY commit_sha
            HAVING
                (bool_or(entity_type = 'deploy' AND action = 'started')
                 AND NOT bool_or(entity_type = 'deploy'
                                 AND action IN ('succeeded', 'failed')))
                OR
                (bool_or(entity_type = 'deploy' AND action = 'succeeded')
                 AND NOT bool_or(entity_type = 'staging_smoke'
                                 AND action IN ('passed', 'failed')))
            """
        ),
        {"shas": shas},
    )
    return {row[0] for row in result.fetchall()}


async def _relation_missing(session: AsyncSession, name: str) -> bool:
    exists = await session.execute(text("SELECT to_regclass(:n)"), {"n": name})
    return exists.scalar_one_or_none() is None


async def _last_activity_at(
    session: AsyncSession, coordinator_label: str
) -> datetime | None:
    """The team's most recent activity: the greatest event ``created_at`` across
    the team's tasks, falling back to the newest team task's ``created_at`` when a
    team has tasks but no events. NULL when the team has no tasks. The idle-sweep
    reads this to apply an idle-grace before tearing an idle-but-clean team down.
    """
    row = await session.execute(
        text(
            """
            WITH team_tasks AS (
                SELECT id, created_at FROM tasks WHERE created_by = :coordinator_label
            )
            SELECT GREATEST(
                (SELECT MAX(e.created_at) FROM events e
                  WHERE e.task_id IN (SELECT id FROM team_tasks)),
                (SELECT MAX(tt.created_at) FROM team_tasks tt)
            )
            """
        ),
        {"coordinator_label": coordinator_label},
    )
    return row.scalar_one_or_none()


async def _plans_with_handoff(
    session: AsyncSession, plan_ids: list[str]
) -> set[str]:
    """Of ``plan_ids``, those with a recorded ``plan.handoff_pr_opened`` event
    (ADR-0110). Keyed on the RECORDED handoff event, never a heuristic scan of open
    PRs — so an unrelated PR into main is never mistaken for the handoff. A plan
    with a handoff event is IMPLEMENTED (the team's last act is done); one without,
    whose tasks are all terminal, still OWES the handoff and blocks teardown."""
    if not plan_ids or await _relation_missing(session, "events"):
        return set()
    result = await session.execute(
        text(
            """
            SELECT DISTINCT plan_id::text
            FROM events
            WHERE entity_type = 'plan' AND action = 'handoff_pr_opened'
              AND plan_id = ANY(:ids)
            """
        ),
        {"ids": plan_ids},
    )
    return {row[0] for row in result.fetchall()}


@router.get("/team_configs/{repo:path}/drain", response_model=DrainStatus)
async def get_team_drain(
    repo: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DrainStatus:
    """ADR-0109 drain-guard: is this team safe to tear down?

    Fail-closed. A task blocks teardown when it is TEAM-ACTIVE — non-terminal and NOT
    escalated (an unknown/new derived_status defaults to blocking). `escalated` tasks
    are PARKED-ON-HUMAN (do not block; tracked for re-standup). A `pr_merged` task is
    terminal UNLESS its merge sha has an unsettled deploy (main-mode: the coordinator
    must stay to observe/escalate; feature-branch merges never deploy → settled).
    """
    cfg = await _store.get_by_repo(session, repo)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_config for repo {repo!r} not found",
        )
    if await _relation_missing(session, "tasks") or await _relation_missing(
        session, "task_status"
    ):
        return DrainStatus(repo=repo, clean=True, blocking=[], parked=[])

    rows = (
        await session.execute(
            _TEAM_DRAIN_SQL, {"coordinator_label": cfg.coordinator_label}
        )
    ).fetchall()

    blocking: list[DrainItem] = []
    parked: list[str] = []
    merged_task_ids: list[str] = []
    # Per-plan bookkeeping for the feature-branch handoff gate (ADR-0110): a plan is
    # "implemented" only when its tasks are all terminal AND the handoff PR is
    # recorded. `all_terminal` stays True only if every task is clean/merged (no
    # active or parked task); `plan_ids` collects the team's plans.
    plan_all_terminal: dict[str, bool] = {}
    for task_id, plan_id, derived_status, escalated in rows:
        cat = _classify(derived_status, escalated)
        terminal = cat in ("clean", "merged")
        if plan_id is not None:
            prev = plan_all_terminal.get(plan_id, True)
            plan_all_terminal[plan_id] = prev and terminal
        if cat == "parked":
            parked.append(task_id)  # parked-on-human: does NOT block
        elif cat == "block":
            blocking.append(
                DrainItem(task_id=task_id, derived_status=derived_status,
                          reason="team_active")
            )
        elif cat == "merged":
            merged_task_ids.append(task_id)  # terminal-merged: check post-merge deploy
        # cat == "clean": terminal (done/validated/cancelled) → allows teardown

    # Post-merge (mode-agnostic): a merged task whose deploy is unsettled still blocks.
    if merged_task_ids:
        shas = await _merge_shas_for_tasks(session, merged_task_ids)
        unsettled = await _unsettled_deploy_shas(session, list(set(shas.values())))
        for tid, sha in shas.items():
            if sha in unsettled:
                blocking.append(
                    DrainItem(task_id=tid, derived_status="pr_merged",
                              reason="post_merge_unsettled")
                )

    # Feature-branch handoff gate (ADR-0110): a plan whose tasks are ALL terminal but
    # has NO recorded handoff PR is implemented-but-not-handed-off — the team still
    # owes the `branch → main` handoff, so it BLOCKS teardown. This is the gate that
    # stops the sweep (ADR-0109) from tearing a team down after its last task merges
    # but before the handoff exists, which would orphan the branch. A plan WITH a
    # handoff event is done (the handoff PR is parked-on-human) and does not block.
    if cfg.merge_target == "feature-branch" and plan_all_terminal:
        terminal_plans = [p for p, done in plan_all_terminal.items() if done]
        if terminal_plans:
            handed_off = await _plans_with_handoff(session, terminal_plans)
            for plan_id in terminal_plans:
                if plan_id not in handed_off:
                    # Plan-scoped block: task_id carries the plan id and the status
                    # says so, so the operator's blocking list reads "plan …
                    # awaiting handoff", not a phantom task (Ernie cosmetic note).
                    blocking.append(
                        DrainItem(
                            task_id=plan_id,
                            derived_status="plan implemented; awaiting branch→main handoff",
                            reason="awaiting_handoff",
                        )
                    )

    last_activity = await _last_activity_at(session, cfg.coordinator_label)
    return DrainStatus(
        repo=repo,
        clean=not blocking,
        blocking=blocking,
        parked=parked,
        last_activity_at=last_activity,
    )


@router.get("/team_configs", response_model=list[TeamConfigRow])
async def list_team_configs(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TeamConfigRow]:
    rows = await _store.list_all(session)
    return [TeamConfigRow.model_validate(r, from_attributes=True) for r in rows]


@router.get("/team_configs/{repo:path}", response_model=TeamConfigRow)
async def get_team_config(
    repo: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TeamConfigRow:
    row = await _store.get_by_repo(session, repo)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_config for repo {repo!r} not found",
        )
    return TeamConfigRow.model_validate(row, from_attributes=True)


@router.delete(
    "/team_configs/{repo:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_team_config(
    repo: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    deleted = await _store.delete(session, repo)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"team_config for repo {repo!r} not found",
        )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_QUEUE_DEPTH_SQL = text(
    """
    SELECT
        COUNT(*) FILTER (WHERE ts.derived_status = 'registered')      AS visible,
        COUNT(*) FILTER (WHERE ts.derived_status LIKE '%: executing') AS in_flight
    FROM task_status ts
    LEFT JOIN tasks t ON t.id = ts.id
    WHERE COALESCE(t.created_by, '') NOT IN (
        SELECT coordinator_label FROM team_configs
    )
    """
)


@router.get("/queue_depth", response_model=QueueDepth)
async def get_queue_depth(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> QueueDepth:
    """Visible + in-flight task counts, excluding coordinator-authored tasks.

    Coordinators emit their own brief tasks via ``created_by =
    <coordinator_label>``. Those tasks already have a routing owner; the
    operator-facing depth shows only tasks that need triage attention.
    """
    result = await session.execute(_QUEUE_DEPTH_SQL)
    row = result.one()
    return QueueDepth(visible=int(row.visible or 0), in_flight=int(row.in_flight or 0))
