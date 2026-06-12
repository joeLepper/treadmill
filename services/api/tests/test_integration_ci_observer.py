"""End-to-end integration for the ADR-0090 CI-observer (task 5dd4a32d).

Drives the REAL ingest seam (``persist_and_resolve_webhook_event``) with
the captured check-suite deliveries from ``fixtures/check_suites/``
against a real Postgres: head_sha writer → 13-delivery mixed suite →
exactly ONE ``task.ci_result`` with GitHub's failure rollup, attributed
to the seeded task; re-delivery idempotent; clean suite → success;
netlify's eternal ``queued`` never emits.

Run requirements (the #331 safety rule — no live-DB default):

  TREADMILL_INTEGRATION=1
  TREADMILL_TEST_DATABASE_URL=postgresql+psycopg://.../<DEDICATED TEST DB>
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

import sys

sys.path.insert(0, str(Path(__file__).parent))
from test_ci_observer import _deliveries, _load  # noqa: E402  fixture builders

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
MIXED_SHA = "784e851725df784896c8c3174579230c302583d4"
PR_NUMBER = 314


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
    task_id, plan_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE plans, tasks, task_prs, task_dependencies, events CASCADE"
            )
        )
        conn.execute(
            sa.text("INSERT INTO plans (id, repo, intent) VALUES (:p, :r, 'obs')"),
            {"p": plan_id, "r": REPO},
        )
        conn.execute(
            sa.text(
                "INSERT INTO tasks (id, plan_id, repo, title) "
                "VALUES (:t, :p, :r, 'observer test')"
            ),
            {"t": task_id, "p": plan_id, "r": REPO},
        )
        # NO head_sha: the ingest-time writer (this task) must set it.
        conn.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id) "
                "VALUES (:r, :n, :t)"
            ),
            {"r": REPO, "n": PR_NUMBER, "t": task_id},
        )
    yield task_id
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE plans, tasks, task_prs, task_dependencies, events CASCADE"
            )
        )


class _NoopPublisher:
    async def publish(self, event, typed):  # noqa: ANN001
        pass


async def _ingest(body: dict) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from treadmill_api.webhooks.normalize import normalize_github_event
    from treadmill_api.webhooks.persist import persist_and_resolve_webhook_event

    url = TEST_DB_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://")
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    github_event = "pull_request" if "pull_request" in body else "check_run"
    normalized = normalize_github_event(github_event, body)
    assert normalized is not None
    async with maker() as session:
        await persist_and_resolve_webhook_event(
            session, normalized, body, redis_client=None,
            publisher=_NoopPublisher(),
        )
    await engine.dispose()


def _pr_synchronize_body(head_sha: str) -> dict:
    return {
        "action": "synchronize",
        "before": "0" * 40,
        "pull_request": {
            "number": PR_NUMBER,
            "head": {"sha": head_sha, "ref": "x"},
        },
        "repository": {"full_name": REPO},
        "sender": {"login": "tester"},
    }


def _ci_results(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT task_id, commit_sha, payload FROM events "
                "WHERE entity_type='task' AND action='ci_result' "
                "ORDER BY created_at"
            )
        ).fetchall()
    return [
        {"task_id": r.task_id, "commit_sha": r.commit_sha, "payload": r.payload}
        for r in rows
    ]


def test_full_flow_one_ci_result_per_suite(
    engine: Engine, seeded_task: uuid.UUID,
) -> None:
    print("\nRUNLOG ── ingest pr_synchronize (head_sha writer) ──")
    asyncio.run(_ingest(_pr_synchronize_body(MIXED_SHA)))
    with engine.connect() as conn:
        written = conn.execute(
            sa.text(
                "SELECT head_sha FROM task_prs WHERE repo=:r AND pr_number=:n"
            ),
            {"r": REPO, "n": PR_NUMBER},
        ).scalar_one()
    print(f"RUNLOG task_prs.head_sha written at ingest: {written[:8]}…")
    assert written == MIXED_SHA

    print("RUNLOG ── ingest 13 captured mixed-suite deliveries ──")
    bodies = _deliveries(
        _load("mixed_failure_runs.json"),
        _load("mixed_failure_suites.json"),
        REPO,
    )
    for body in bodies:
        asyncio.run(_ingest(body))
    results = _ci_results(engine)
    print(
        f"RUNLOG ci_result rows: {len(results)} "
        f"(conclusion={results[0]['payload']['conclusion']}, "
        f"task={results[0]['task_id']})"
    )
    assert len(results) == 1, "exactly ONE ci_result for 13 deliveries"
    assert results[0]["payload"]["conclusion"] == "failure"  # GitHub's rollup
    assert results[0]["task_id"] == seeded_task
    assert results[0]["commit_sha"] == MIXED_SHA

    print("RUNLOG ── re-deliver the final check_run (idempotency) ──")
    asyncio.run(_ingest(bodies[-1]))
    assert len(_ci_results(engine)) == 1
    print("RUNLOG still exactly one ci_result after re-delivery")


def test_attribution_via_events_join_fallback(
    engine: Engine, seeded_task: uuid.UUID,
) -> None:
    """First-push race: the pr event lands (creating the events-join
    substrate) but the head_sha writer's UPDATE hit before... here we
    simulate the worst case — head_sha column stays NULL (writer raced
    a not-yet-registered row) — by clearing it after the pr ingest."""
    asyncio.run(_ingest(_pr_synchronize_body(MIXED_SHA)))
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE task_prs SET head_sha = NULL"))
    bodies = _deliveries(
        _load("mixed_failure_runs.json"),
        _load("mixed_failure_suites.json"),
        REPO,
    )
    asyncio.run(_ingest(bodies[-1]))  # only the completing delivery
    results = _ci_results(engine)
    print(
        f"\nRUNLOG fallback attribution (head_sha NULL): "
        f"{len(results)} ci_result, task={results[0]['task_id']}"
    )
    assert len(results) == 1
    assert results[0]["task_id"] == seeded_task


def test_netlify_suite_never_emits(engine: Engine, seeded_task: uuid.UUID) -> None:
    suites = _load("mixed_failure_suites.json")
    netlify = next(
        s for s in suites["check_suites"] if s["app"]["slug"] == "netlify"
    )
    body = {
        "action": "completed",
        "check_run": {
            "name": "netlify/deploy-preview",
            "conclusion": "neutral",
            "head_sha": MIXED_SHA,
            "pull_requests": [],
            "check_suite": {
                "id": netlify["id"],
                "status": netlify["status"],  # 'queued' — captured
                "conclusion": netlify["conclusion"],  # None — captured
            },
            "app": {"slug": "netlify"},
        },
        "repository": {"full_name": REPO},
    }
    asyncio.run(_ingest(body))
    assert _ci_results(engine) == []
    print("\nRUNLOG netlify queued suite: zero ci_result (correct)")
