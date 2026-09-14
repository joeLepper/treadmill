"""DB-backed foils for the drain-guard (ADR-0109). A pure test cannot cover these —
they run the real SQL against real rows.

One foil per untested link in the drain-guard's single safety chain:
  1. ``_unsettled_deploy_shas`` — the two DISTINCT event streams (deploy:
     started/succeeded/failed; staging_smoke: passed/failed, NO 'started'), settled
     PER stream (Ernie BLOCKING 2).
  2. the escalation open-vs-closed CTE in ``_TEAM_DRAIN_SQL`` — the drain-SPECIFIC
     combining boolean (open iff latest escalation is followed by no later ack/close),
     which overview.py's aggregate test does NOT exercise (Ernie BLOCKING 1a).
  3. the WIRED endpoint ``GET /drain`` end-to-end — proves ``_merge_shas_for_tasks``
     hands the deploy check the RIGHT sha and the parts compose (Ernie BLOCKING 1b).
  4. ``_last_activity_at`` — the idle-sweep's age signal: max event time across the
     team's tasks, falling back to the newest task's creation, NULL when no tasks.

All gated on the integration DB.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from treadmill_api.routers.team_configs import (
    _TEAM_DRAIN_SQL,
    _last_activity_at,
    _unsettled_deploy_shas,
    get_team_drain,
)

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
integration = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason="set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL to run",
)


@pytest.fixture(scope="module")
def database_url() -> str:
    return TEST_DB_URL  # type: ignore[return-value]


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str) -> None:
    if not (INTEGRATION and TEST_DB_URL):
        return
    services_api_dir = Path(__file__).resolve().parent.parent
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


@pytest_asyncio.fixture
async def session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async_engine = create_async_engine(database_url.replace("+psycopg", "+asyncpg"))
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    yield factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Seed helpers (async, minimal — only the columns the drain path reads).
# ---------------------------------------------------------------------------

_BASE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


async def _emit(
    session: AsyncSession,
    entity_type: str,
    action: str,
    *,
    sha: str | None = None,
    task_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> None:
    await session.execute(
        sa.text(
            "INSERT INTO events (entity_type, action, commit_sha, task_id, created_at) "
            "VALUES (:et, :ac, :sha, :tid, COALESCE(:at, now()))"
        ),
        {"et": entity_type, "ac": action, "sha": sha, "tid": task_id, "at": at},
    )


async def _seed_plan(session: AsyncSession, repo: str) -> uuid.UUID:
    row = await session.execute(
        sa.text("INSERT INTO plans (repo) VALUES (:r) RETURNING id"), {"r": repo}
    )
    return row.scalar_one()


async def _seed_task(
    session: AsyncSession, plan_id: uuid.UUID, created_by: str, repo: str
) -> uuid.UUID:
    row = await session.execute(
        sa.text(
            "INSERT INTO tasks (plan_id, repo, title, created_by) "
            "VALUES (:p, :r, 'drain foil', :c) RETURNING id"
        ),
        {"p": plan_id, "r": repo, "c": created_by},
    )
    return row.scalar_one()


@integration
@pytest.mark.asyncio
async def test_post_merge_deploy_settling_per_stream(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exact cases from the BLOCKING-2 review — per stream, against real events."""
    p = uuid.uuid4().hex[:8]
    sha_started = f"{p}-started"  # deploy started, no terminal → UNSETTLED
    sha_ok_no_smoke = f"{p}-ok-no-smoke"  # deploy succeeded, no smoke → UNSETTLED (pending)
    sha_full = f"{p}-full"  # deploy succeeded + smoke passed → settled
    sha_deploy_failed = f"{p}-dfail"  # deploy failed → settled (failure is terminal)
    sha_smoke_failed = f"{p}-sfail"  # deploy succeeded + smoke failed → settled
    sha_none = f"{p}-none"  # no deploy events (feature-branch) → settled

    async with session_factory() as session:
        await _emit(session, "deploy", "started", sha=sha_started)

        await _emit(session, "deploy", "started", sha=sha_ok_no_smoke)
        await _emit(session, "deploy", "succeeded", sha=sha_ok_no_smoke)

        await _emit(session, "deploy", "started", sha=sha_full)
        await _emit(session, "deploy", "succeeded", sha=sha_full)
        await _emit(session, "staging_smoke", "passed", sha=sha_full)

        await _emit(session, "deploy", "started", sha=sha_deploy_failed)
        await _emit(session, "deploy", "failed", sha=sha_deploy_failed)

        await _emit(session, "deploy", "succeeded", sha=sha_smoke_failed)
        await _emit(session, "staging_smoke", "failed", sha=sha_smoke_failed)
        await session.commit()

        all_shas = [
            sha_started,
            sha_ok_no_smoke,
            sha_full,
            sha_deploy_failed,
            sha_smoke_failed,
            sha_none,
        ]
        unsettled = await _unsettled_deploy_shas(session, all_shas)

    # Only a deploy still running, or a successful deploy whose smoke has not landed,
    # keeps the coordinator on the hook — the smoke-leg gap the review caught.
    assert unsettled == {sha_started, sha_ok_no_smoke}, unsettled


@integration
@pytest.mark.asyncio
async def test_escalation_open_vs_closed_cte(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The drain-SPECIFIC open-escalation boolean: escalated iff the LATEST
    escalation is followed by NO later ack and NO later close. This is the
    parked-vs-block safety boundary, and it hinges on timestamp comparisons the
    dashboard's aggregate test never exercises (Ernie BLOCKING 1a)."""
    coord = f"coordinator-esc-{uuid.uuid4().hex[:8]}"
    repo = f"o/esc-{uuid.uuid4().hex[:6]}"

    async with session_factory() as session:
        plan_id = await _seed_plan(session, repo)

        # task_open: escalated, never acked/closed → escalated=True (parked).
        t_open = await _seed_task(session, plan_id, coord, repo)
        await _emit(session, "task", "escalated_to_operator", task_id=t_open, at=_BASE)

        # task_acked: escalated then a LATER ack → escalated=False.
        t_acked = await _seed_task(session, plan_id, coord, repo)
        await _emit(session, "task", "escalated_to_operator", task_id=t_acked, at=_BASE)
        await _emit(
            session,
            "task",
            "escalation_acknowledged",
            task_id=t_acked,
            at=_BASE + timedelta(minutes=5),
        )

        # task_closed: escalated then a LATER close → escalated=False.
        t_closed = await _seed_task(session, plan_id, coord, repo)
        await _emit(session, "task", "escalated_to_operator", task_id=t_closed, at=_BASE)
        await _emit(
            session,
            "task",
            "escalation_closed",
            task_id=t_closed,
            at=_BASE + timedelta(minutes=5),
        )

        # task_reescalated: escalated, closed, then escalated AGAIN (latest) → True.
        t_re = await _seed_task(session, plan_id, coord, repo)
        await _emit(session, "task", "escalated_to_operator", task_id=t_re, at=_BASE)
        await _emit(
            session,
            "task",
            "escalation_closed",
            task_id=t_re,
            at=_BASE + timedelta(minutes=5),
        )
        await _emit(
            session,
            "task",
            "escalated_to_operator",
            task_id=t_re,
            at=_BASE + timedelta(minutes=10),
        )

        # task_none: no escalation events at all → escalated=False.
        t_none = await _seed_task(session, plan_id, coord, repo)
        await session.commit()

        rows = (await session.execute(_TEAM_DRAIN_SQL, {"coordinator_label": coord})).fetchall()

    escalated_by_task = {r.task_id: r.escalated for r in rows}
    assert escalated_by_task[str(t_open)] is True, escalated_by_task
    assert escalated_by_task[str(t_acked)] is False, escalated_by_task
    assert escalated_by_task[str(t_closed)] is False, escalated_by_task
    assert escalated_by_task[str(t_re)] is True, escalated_by_task
    assert escalated_by_task[str(t_none)] is False, escalated_by_task


@integration
@pytest.mark.asyncio
async def test_get_team_drain_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The WIRED endpoint. Proves the parts COMPOSE — the piece-tests prove the
    parts. Critically, this is the foil that catches a wrong task→sha link: if
    ``_merge_shas_for_tasks`` reads the wrong event, the merged-but-unsettled task
    silently drops out of ``blocking`` and the guard tears a team down with
    unobserved deploy work (Ernie BLOCKING 1b)."""
    coord = f"coordinator-e2e-{uuid.uuid4().hex[:8]}"
    repo = f"o/e2e-{uuid.uuid4().hex[:6]}"
    sha_settled = f"e2e-{uuid.uuid4().hex[:8]}-ok"
    sha_unsettled = f"e2e-{uuid.uuid4().hex[:8]}-run"

    async with session_factory() as session:
        plan_id = await _seed_plan(session, repo)
        await session.execute(
            sa.text("INSERT INTO team_configs (repo, coordinator_label) VALUES (:r, :c)"),
            {"r": repo, "c": coord},
        )

        # t_active: no task_execution, no events → derived_status 'registered' →
        # team-active → BLOCK.
        await _seed_task(session, plan_id, coord, repo)

        # t_parked: escalated, open → parked (does NOT block).
        t_parked = await _seed_task(session, plan_id, coord, repo)
        await _emit(session, "task", "escalated_to_operator", task_id=t_parked, at=_BASE)

        # t_merged_settled: github.pr_merged → 'pr_merged'; deploy fully settled →
        # NOT blocking.
        t_ok = await _seed_task(session, plan_id, coord, repo)
        await _emit(session, "github", "pr_merged", sha=sha_settled, task_id=t_ok)
        await _emit(session, "deploy", "started", sha=sha_settled)
        await _emit(session, "deploy", "succeeded", sha=sha_settled)
        await _emit(session, "staging_smoke", "passed", sha=sha_settled)

        # t_merged_unsettled: github.pr_merged → 'pr_merged'; deploy started but not
        # terminal → MUST block (post_merge_unsettled). This is the case a wrong
        # task→sha link (the entity_type bug) would silently drop.
        t_bad = await _seed_task(session, plan_id, coord, repo)
        await _emit(session, "github", "pr_merged", sha=sha_unsettled, task_id=t_bad)
        await _emit(session, "deploy", "started", sha=sha_unsettled)
        await session.commit()

        drain = await get_team_drain(repo=repo, session=session)

    assert drain.clean is False, drain
    blocking_ids = {item.task_id for item in drain.blocking}
    reasons = {item.task_id: item.reason for item in drain.blocking}
    # The registered task blocks as team-active.
    assert any(r == "team_active" for r in reasons.values()), reasons
    # The merged-but-unsettled task blocks as post_merge_unsettled — the sha was
    # correctly resolved from its github.pr_merged event and found unsettled.
    assert str(t_bad) in blocking_ids, drain
    assert reasons[str(t_bad)] == "post_merge_unsettled", reasons
    # The escalated task is parked, not blocking.
    assert drain.parked == [str(t_parked)], drain
    assert str(t_parked) not in blocking_ids, drain
    # The fully-settled merged task neither blocks nor parks.
    assert str(t_ok) not in blocking_ids, drain


@integration
@pytest.mark.asyncio
async def test_last_activity_at_is_max_event_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """last_activity_at is the greatest event created_at across the team's tasks —
    the real activity signal the idle-sweep ages against. The sweep writes no
    events, so it cannot spoof this by probing."""
    coord = f"coordinator-act-{uuid.uuid4().hex[:8]}"
    repo = f"o/act-{uuid.uuid4().hex[:6]}"
    t_created = _BASE - timedelta(hours=1)
    t_early = _BASE
    t_late = _BASE + timedelta(hours=3)

    async with session_factory() as session:
        plan_id = await _seed_plan(session, repo)
        # Explicit task created_at BEFORE the events (production order), so the max
        # event time is the true latest activity, not the task's creation.
        task = (
            await session.execute(
                sa.text(
                    "INSERT INTO tasks (plan_id, repo, title, created_by, created_at) "
                    "VALUES (:p, :r, 'act', :c, :ts) RETURNING id"
                ),
                {"p": plan_id, "r": repo, "c": coord, "ts": t_created},
            )
        ).scalar_one()
        await _emit(session, "task", "assigned", task_id=task, at=t_early)
        await _emit(session, "task", "pr_merged", task_id=task, at=t_late)
        await session.commit()
        last = await _last_activity_at(session, coord)

    assert last is not None
    assert last.replace(tzinfo=None) == t_late.replace(tzinfo=None), last


@integration
@pytest.mark.asyncio
async def test_last_activity_at_falls_back_to_task_created(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A team with tasks but no events ages against the newest task's creation,
    not NULL — so a team that registered tasks but never emitted an event is still
    aged (and swept once genuinely idle), not left standing forever."""
    coord = f"coordinator-fb-{uuid.uuid4().hex[:8]}"
    repo = f"o/fb-{uuid.uuid4().hex[:6]}"
    created = _BASE - timedelta(hours=10)

    async with session_factory() as session:
        plan_id = await _seed_plan(session, repo)
        # Seed a task with an explicit created_at (no events for it).
        await session.execute(
            sa.text(
                "INSERT INTO tasks (plan_id, repo, title, created_by, created_at) "
                "VALUES (:p, :r, 'fb', :c, :ts)"
            ),
            {"p": plan_id, "r": repo, "c": coord, "ts": created},
        )
        await session.commit()
        last = await _last_activity_at(session, coord)

    assert last is not None
    assert last.replace(tzinfo=None) == created.replace(tzinfo=None), last


@integration
@pytest.mark.asyncio
async def test_last_activity_at_none_when_no_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A team with no tasks has no age → NULL. The sweep leaves such a team alone
    (it may be freshly stood up awaiting its first plan)."""
    coord = f"coordinator-empty-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        last = await _last_activity_at(session, coord)
    assert last is None


# ---------------------------------------------------------------------------
# ADR-0110 feature-branch handoff gate (step 3). A feature-branch plan is
# "implemented" only when its tasks are all terminal AND the handoff PR is
# recorded; the drain-guard must BLOCK an all-terminal plan that has no handoff
# yet (else the sweep orphans the branch), and NOT block once the handoff event
# exists. Keyed on the RECORDED handoff event, never a heuristic PR scan.
# ---------------------------------------------------------------------------


async def _seed_team_config(
    session: AsyncSession, repo: str, coord: str, merge_target: str
) -> None:
    await session.execute(
        sa.text(
            "INSERT INTO team_configs (repo, coordinator_label, merge_target) "
            "VALUES (:r, :c, :mt)"
        ),
        {"r": repo, "c": coord, "mt": merge_target},
    )


async def _seed_handoff(
    session: AsyncSession, plan_id: uuid.UUID, repo: str
) -> None:
    payload = json.dumps(
        {"repo": repo, "branch": "joes-agents/x", "pr_url": "u", "pr_number": 9}
    )
    await session.execute(
        sa.text(
            "INSERT INTO events (entity_type, action, plan_id, payload) "
            "VALUES ('plan', 'handoff_pr_opened', :pid, CAST(:pl AS jsonb))"
        ),
        {"pid": plan_id, "pl": payload},
    )


@integration
@pytest.mark.asyncio
async def test_feature_branch_all_terminal_no_handoff_blocks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ernie #4: all tasks integrated (pr_merged into the branch) but NO handoff
    recorded → the plan is implemented-but-not-handed-off → BLOCKS teardown, so the
    sweep cannot orphan the branch."""
    coord = f"coordinator-fbh-{uuid.uuid4().hex[:8]}"
    repo = f"o/fbh-{uuid.uuid4().hex[:6]}"
    async with session_factory() as session:
        plan_id = await _seed_plan(session, repo)
        await _seed_team_config(session, repo, coord, "feature-branch")
        t1 = await _seed_task(session, plan_id, coord, repo)
        await _emit(session, "github", "pr_merged", sha="fbh1", task_id=t1)
        await session.commit()
        drain = await get_team_drain(repo=repo, session=session)

    assert drain.clean is False, drain
    reasons = {i.task_id: i.reason for i in drain.blocking}
    assert reasons.get(str(plan_id)) == "awaiting_handoff", drain


@integration
@pytest.mark.asyncio
async def test_feature_branch_handoff_recorded_does_not_block(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With the handoff event recorded, the plan is implemented (the handoff PR is
    parked-on-human) → teardown proceeds."""
    coord = f"coordinator-fbok-{uuid.uuid4().hex[:8]}"
    repo = f"o/fbok-{uuid.uuid4().hex[:6]}"
    async with session_factory() as session:
        plan_id = await _seed_plan(session, repo)
        await _seed_team_config(session, repo, coord, "feature-branch")
        t1 = await _seed_task(session, plan_id, coord, repo)
        await _emit(session, "github", "pr_merged", sha="fbok1", task_id=t1)
        await _seed_handoff(session, plan_id, repo)
        await session.commit()
        drain = await get_team_drain(repo=repo, session=session)

    assert drain.clean is True, drain


@integration
@pytest.mark.asyncio
async def test_feature_branch_active_task_blocks_not_handoff_gate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ernie #3: an open task PR (team-active) BLOCKS via the normal path — and the
    plan is NOT all-terminal, so the handoff gate does not also fire. The two are
    distinct: the handoff is keyed on its recorded event, not on any open PR."""
    coord = f"coordinator-fbact-{uuid.uuid4().hex[:8]}"
    repo = f"o/fbact-{uuid.uuid4().hex[:6]}"
    async with session_factory() as session:
        plan_id = await _seed_plan(session, repo)
        await _seed_team_config(session, repo, coord, "feature-branch")
        t_active = await _seed_task(session, plan_id, coord, repo)  # registered
        await session.commit()
        drain = await get_team_drain(repo=repo, session=session)

    assert drain.clean is False, drain
    reasons = {i.task_id: i.reason for i in drain.blocking}
    assert reasons.get(str(t_active)) == "team_active", drain
    assert "awaiting_handoff" not in reasons.values(), drain


@integration
@pytest.mark.asyncio
async def test_main_mode_all_terminal_does_not_await_handoff(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The handoff gate is feature-branch ONLY. A main-mode plan whose tasks are all
    terminal (and deploy settled) is clean — no handoff concept applies."""
    coord = f"coordinator-mm-{uuid.uuid4().hex[:8]}"
    repo = f"o/mm-{uuid.uuid4().hex[:6]}"
    async with session_factory() as session:
        plan_id = await _seed_plan(session, repo)
        await _seed_team_config(session, repo, coord, "main")
        t1 = await _seed_task(session, plan_id, coord, repo)
        # pr_merged + deploy fully settled → terminal, main-mode.
        await _emit(session, "github", "pr_merged", sha="mm1", task_id=t1)
        await _emit(session, "deploy", "started", sha="mm1")
        await _emit(session, "deploy", "succeeded", sha="mm1")
        await _emit(session, "staging_smoke", "passed", sha="mm1")
        await session.commit()
        drain = await get_team_drain(repo=repo, session=session)

    assert drain.clean is True, drain
