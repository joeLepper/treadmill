"""DB-backed foil for the PR-state poll-ingest endpoint (ADR-0113). Proves a poller's
observed merge, routed through the shared webhook seam, produces a `github.pr_merged`
event BYTE-IDENTICAL to a real delivery — task_id resolved from `(repo, pr_number)` via
task_prs, `events.commit_sha` = the merge sha (ADR-0014) — and that a re-poll is a
no-op (deterministic event_id upsert). Gated on the integration DB.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from treadmill_api.routers.github import (
    PollCheckRunIngestRequest,
    PollIngestRequest,
    poll_ingest,
    poll_ingest_check_run,
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


def _fake_request() -> SimpleNamespace:
    # The endpoint only reads request.app.state.redis (None → seam skips buffering).
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))


@integration
@pytest.mark.asyncio
async def test_poll_ingest_pr_merged_is_identical_to_a_webhook(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = f"o/poll-{uuid.uuid4().hex[:6]}"
    pr_number = 4242
    merge_sha = uuid.uuid4().hex

    async with session_factory() as session:
        plan_id = (
            await session.execute(
                sa.text("INSERT INTO plans (repo) VALUES (:r) RETURNING id"),
                {"r": repo},
            )
        ).scalar_one()
        task_id = (
            await session.execute(
                sa.text(
                    "INSERT INTO tasks (plan_id, repo, title) "
                    "VALUES (:p, :r, 'poll foil') RETURNING id"
                ),
                {"p": plan_id, "r": repo},
            )
        ).scalar_one()
        # The coordinator registered the PR when the worker relayed it (webhook-free).
        await session.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id) "
                "VALUES (:r, :n, :t)"
            ),
            {"r": repo, "n": pr_number, "t": task_id},
        )
        await session.commit()

        resp = await poll_ingest(
            PollIngestRequest(
                repo=repo, pr_number=pr_number, action="pr_merged", merge_sha=merge_sha
            ),
            session=session,
            request=_fake_request(),
        )
        assert resp.entity_type == "github"
        assert resp.action == "pr_merged"

        # The persisted event is a real github.pr_merged with task_id resolved AND the
        # commit_sha COLUMN set — exactly what the drain-guard + task_status view read.
        row = (
            await session.execute(
                sa.text(
                    "SELECT task_id::text, commit_sha FROM events "
                    "WHERE entity_type='github' AND action='pr_merged' "
                    "AND task_id = :t"
                ),
                {"t": task_id},
            )
        ).one()
    assert row[0] == str(task_id)
    assert row[1] == merge_sha  # ADR-0014 commit-anchor extraction ran


@integration
@pytest.mark.asyncio
async def test_poll_ingest_is_idempotent_on_re_poll(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-poll of the same merge must NOT re-publish (the coordinator must not
    re-process it). The shared seam publishes unconditionally, so the endpoint's own
    existence gate is what prevents it. Proven by counting publishes: the seam runs +
    publishes exactly ONCE across two ingests, and only one event row exists."""
    import treadmill_api.routers.github as gh_router

    published: list = []

    class _CountingPublisher:
        def publish(self, event, typed=None):  # seam calls publisher.publish(event, typed)
            published.append(event)

    monkeypatch.setattr(gh_router, "get_publisher", lambda: _CountingPublisher())

    repo = f"o/idem-{uuid.uuid4().hex[:6]}"
    pr_number = 7
    merge_sha = uuid.uuid4().hex
    body = PollIngestRequest(
        repo=repo, pr_number=pr_number, action="pr_merged", merge_sha=merge_sha
    )

    async with session_factory() as session:
        plan_id = (
            await session.execute(
                sa.text("INSERT INTO plans (repo) VALUES (:r) RETURNING id"),
                {"r": repo},
            )
        ).scalar_one()
        task_id = (
            await session.execute(
                sa.text(
                    "INSERT INTO tasks (plan_id, repo, title) "
                    "VALUES (:p, :r, 'idem') RETURNING id"
                ),
                {"p": plan_id, "r": repo},
            )
        ).scalar_one()
        await session.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id) VALUES (:r, :n, :t)"
            ),
            {"r": repo, "n": pr_number, "t": task_id},
        )
        await session.commit()

        r1 = await poll_ingest(body, session=session, request=_fake_request())
        r2 = await poll_ingest(body, session=session, request=_fake_request())
        assert r1.event_id == r2.event_id  # same deterministic id
        # The SECOND ingest must be a true no-op: it did NOT re-run the seam and did NOT
        # re-publish (which would make the coordinator re-process the merge). The seam
        # publishes unconditionally, so we rely on the endpoint's own existence gate.
        assert r1.already_ingested is False
        assert r2.already_ingested is True
        assert len(published) == 1  # published once, NOT re-published on the re-poll

        count = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM events WHERE entity_type='github' "
                    "AND action='pr_merged' AND task_id = :t"
                ),
                {"t": task_id},
            )
        ).scalar_one()
    assert count == 1  # exactly one row despite two ingests


# --- CI leg (ADR-0113 slice 2) -------------------------------------------------


def test_ci_leg_requires_pr_number() -> None:
    """pr_number is REQUIRED on the CI-leg request (Ernie, slice-3 hardening): the
    endpoint's head_sha writer keys on it, so an omitted number would silently no-op
    the attribution fix. Requiring it makes the fix non-bypassable. No DB needed."""
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        PollCheckRunIngestRequest(  # type: ignore[call-arg]
            repo="o/r",
            head_sha="abc",
            check_suite_id=1,
            conclusion="success",
            app_slug="ga",
        )


async def _seed_task_with_pr(
    session: AsyncSession, repo: str, pr_number: int
) -> uuid.UUID:
    """Register a task + its task_prs row EXACTLY as the coordinator does on a poll
    repo: (repo, pr_number, task_id) with head_sha LEFT NULL. On a webhookless repo
    nothing else populates head_sha (no pr_opened/pr_synchronize webhook, no seam
    writer), so the CI-leg endpoint's OWN head_sha write is what lets the observer's
    resolve_task_by_head_sha attribute the ci_result. Seeding NULL keeps that
    load-bearing: a foil that pre-set head_sha would MASK an attribution regression."""
    plan_id = (
        await session.execute(
            sa.text("INSERT INTO plans (repo) VALUES (:r) RETURNING id"), {"r": repo}
        )
    ).scalar_one()
    task_id = (
        await session.execute(
            sa.text(
                "INSERT INTO tasks (plan_id, repo, title) "
                "VALUES (:p, :r, 'ci foil') RETURNING id"
            ),
            {"p": plan_id, "r": repo},
        )
    ).scalar_one()
    # head_sha deliberately omitted → NULL, the real poll-repo registration state.
    await session.execute(
        sa.text(
            "INSERT INTO task_prs (repo, pr_number, task_id) VALUES (:r, :n, :t)"
        ),
        {"r": repo, "n": pr_number, "t": task_id},
    )
    await session.commit()
    return task_id


@integration
@pytest.mark.asyncio
async def test_poll_ingest_check_run_derives_ci_result_like_a_webhook(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A poller's observed COMPLETED SUITE, routed through the shared seam, must
    reconstruct the check_run_completed body faithfully (integer check_suite.id +
    app.slug in the embedded snapshot) AND make the ci_observer derive the SAME
    task.ci_result a real webhook would — task_id resolved by head_sha, commit_sha =
    head_sha, suite id + conclusion + app_slug carried."""
    repo = f"o/ci-{uuid.uuid4().hex[:6]}"
    pr_number = 51
    head_sha = uuid.uuid4().hex
    suite_id = 918273645
    app_slug = "github-actions"

    async with session_factory() as session:
        task_id = await _seed_task_with_pr(session, repo, pr_number)

        resp = await poll_ingest_check_run(
            PollCheckRunIngestRequest(
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                check_suite_id=suite_id,
                conclusion="success",
                app_slug=app_slug,
            ),
            session=session,
            request=_fake_request(),
        )
        assert resp.entity_type == "github"
        assert resp.action == "check_run_completed"
        assert resp.already_ingested is False

        # The synthesized github.check_run_completed carries the integer suite id +
        # app slug the observer needs — the reconstruction faithfulness Ernie foils.
        cr = (
            await session.execute(
                sa.text(
                    "SELECT commit_sha, payload->>'check_suite_id', "
                    "payload->>'app_slug', payload->>'suite_status' FROM events "
                    "WHERE entity_type='github' AND action='check_run_completed' "
                    "AND commit_sha = :h"
                ),
                {"h": head_sha},
            )
        ).one()
        assert cr[0] == head_sha
        assert cr[1] == str(suite_id)  # integer suite id survived the round-trip
        assert cr[2] == app_slug
        assert cr[3] == "completed"  # the snapshot the observer keys on

        # The observer derived ONE task.ci_result — identical to the webhook path:
        # attributed to the task by head_sha, suite id + conclusion + app_slug carried.
        ci = (
            await session.execute(
                sa.text(
                    "SELECT task_id::text, commit_sha, payload->>'conclusion', "
                    "payload->>'check_suite_id', payload->>'app_slug' FROM events "
                    "WHERE entity_type='task' AND action='ci_result' AND task_id = :t"
                ),
                {"t": task_id},
            )
        ).one()
    assert ci[0] == str(task_id)
    assert ci[1] == head_sha
    assert ci[2] == "success"
    assert ci[3] == str(suite_id)
    assert ci[4] == app_slug


@integration
@pytest.mark.asyncio
async def test_poll_ingest_check_run_re_emits_on_conclusion_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A CI RE-RUN whose conclusion CHANGES must re-emit; a re-poll of the SAME
    conclusion must not. Dedup is keyed on (check_suite_id, head_sha, conclusion), so:
    success → one ci_result; re-poll success → no-op (already_ingested); the suite
    re-runs to failure → a NEW ci_result the coordinator needs. Two ci_result rows."""
    repo = f"o/cirerun-{uuid.uuid4().hex[:6]}"
    pr_number = 9
    head_sha = uuid.uuid4().hex
    suite_id = 555000111

    def _body(conclusion: str) -> PollCheckRunIngestRequest:
        return PollCheckRunIngestRequest(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            check_suite_id=suite_id,
            conclusion=conclusion,
            app_slug="github-actions",
        )

    async with session_factory() as session:
        task_id = await _seed_task_with_pr(session, repo, pr_number)

        r_ok1 = await poll_ingest_check_run(
            _body("success"), session=session, request=_fake_request()
        )
        # Same suite+sha+conclusion re-poll: a true no-op (existence gate short-circuits
        # BEFORE the seam, so the observer is not re-run and nothing re-publishes).
        r_ok2 = await poll_ingest_check_run(
            _body("success"), session=session, request=_fake_request()
        )
        # The suite re-runs and now FAILS: a new (suite,sha,conclusion) key → re-emit.
        r_fail = await poll_ingest_check_run(
            _body("failure"), session=session, request=_fake_request()
        )
        assert r_ok1.already_ingested is False
        assert r_ok2.already_ingested is True
        assert r_ok1.event_id == r_ok2.event_id  # same deterministic id
        assert r_fail.already_ingested is False
        assert r_fail.event_id != r_ok1.event_id  # conclusion change → distinct id

        cr_count = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM events WHERE entity_type='github' "
                    "AND action='check_run_completed' AND commit_sha = :h"
                ),
                {"h": head_sha},
            )
        ).scalar_one()
        ci_rows = (
            await session.execute(
                sa.text(
                    "SELECT payload->>'conclusion' FROM events WHERE entity_type='task' "
                    "AND action='ci_result' AND task_id = :t ORDER BY created_at"
                ),
                {"t": task_id},
            )
        ).scalars().all()
    assert cr_count == 2  # success + failure, NOT the redundant success re-poll
    assert list(ci_rows) == ["success", "failure"]  # conclusion-change re-emit


@integration
@pytest.mark.asyncio
async def test_list_task_prs_returns_open_poll_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The poll set (GET /api/v1/task_prs) returns only OPEN rows by default, with
    head_sha, newest first — so the poller polls open PRs and drops closed ones. A
    closed row is excluded unless open=false is requested."""
    from treadmill_api.routers.task_prs import list_task_prs

    repo = f"o/set-{uuid.uuid4().hex[:6]}"
    async with session_factory() as session:
        plan_id = (
            await session.execute(
                sa.text("INSERT INTO plans (repo) VALUES (:r) RETURNING id"), {"r": repo}
            )
        ).scalar_one()

        async def _mk_pr(pr_number: int, *, closed: bool, head: str | None) -> None:
            task_id = (
                await session.execute(
                    sa.text(
                        "INSERT INTO tasks (plan_id, repo, title) "
                        "VALUES (:p, :r, 'set') RETURNING id"
                    ),
                    {"p": plan_id, "r": repo},
                )
            ).scalar_one()
            closed_at = "now()" if closed else "NULL"
            await session.execute(
                sa.text(
                    "INSERT INTO task_prs (repo, pr_number, task_id, head_sha, closed_at) "
                    f"VALUES (:r, :n, :t, :h, {closed_at})"
                ),
                {"r": repo, "n": pr_number, "t": task_id, "h": head},
            )

        await _mk_pr(10, closed=False, head="aaa10")
        await _mk_pr(20, closed=False, head="bbb20")
        await _mk_pr(5, closed=True, head="ccc05")  # merged/closed — not in the set
        await session.commit()

        open_set = await list_task_prs(session=session, repo=repo, open_only=True)
        all_set = await list_task_prs(session=session, repo=repo, open_only=False)

    open_prs = [(p.pr_number, p.head_sha) for p in open_set.task_prs]
    assert open_prs == [(20, "bbb20"), (10, "aaa10")]  # open only, newest first, head
    assert {p.pr_number for p in all_set.task_prs} == {5, 10, 20}  # open=false → all
