"""End-to-end submit regression for task 56c0b353 (PR #327 rework item A).

A plan doc that OMITS the deprecated ``workflow:``/``validation:``
fields must submit clean end to end — parse → plan row → task spawn —
not merely parse. The first cut of this regression lived in
``test_integration_plans_router.py`` and was unexecutable (Phase-5-stale
truncate fixture + live-API dependency); this rewrite is fully
self-contained: the app runs IN-PROCESS via ``TestClient`` with only the
``get_engine``/``get_dispatcher`` seams overridden, against a real
throwaway Postgres with migrations applied. No live API. No shared dev
database.

Gate (per the #331 pattern): ``TREADMILL_TEST_DATABASE_URL`` must name a
DEDICATED throwaway database — this module INSERTs real rows and applies
migrations; never point it at the dev DB.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason=(
        "set TREADMILL_TEST_DATABASE_URL (a DEDICATED throwaway test "
        "database) to run"
    ),
)


_MINIMAL_DOC = """# Plan: minimal post-Phase-5 doc

## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "Minimal modern task"
    intent: Post-Phase-5 doc with neither workflow nor validation.
    scope:
      files: [a.py]
```
"""


def _async_url(url: str) -> str:
    """Normalize any psycopg/bare scheme to the asyncpg driver the app
    engine uses (alembic below keeps the sync driver from the env)."""
    base = url.split("://", 1)[1]
    return f"postgresql+asyncpg://{base}"


@pytest.fixture(scope="module")
def migrated_db_url() -> str:
    """Apply migrations to the throwaway DB once per module."""
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": TEST_DB_URL}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )
    return TEST_DB_URL


@pytest.fixture()
def client(migrated_db_url: str) -> Iterator[TestClient]:  # noqa: F821
    """The plans router IN-PROCESS against the throwaway DB.

    Only the app-state seams are overridden (engine + dispatcher with a
    logging publisher); session wiring, parsers, task spawning, and the
    derived-status VIEW read are all the production code paths.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    from treadmill_api.dependencies_db import get_engine
    from treadmill_api.dispatch import Dispatcher, get_dispatcher
    from treadmill_api.eventbus import LoggingEventPublisher
    from treadmill_api.routers.plans import router as plans_router

    # NullPool: every session opens/closes its own connection on the
    # loop it runs on, so nothing pooled on TestClient's loop survives
    # to trip a cross-loop dispose at teardown.
    engine = create_async_engine(_async_url(migrated_db_url), poolclass=NullPool)
    app = FastAPI()
    app.include_router(plans_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_dispatcher] = lambda: Dispatcher(
        LoggingEventPublisher()
    )
    with TestClient(app) as c:
        c.seed_team_config = lambda repo: _seed_team_config(engine, repo)
        yield c


def _seed_team_config(engine, repo: str) -> None:
    """The doc-driven submit fails closed (412) without a registered
    team — seed the one row the route requires. Direct SQL keeps the
    seam independent of the team_configs API surface."""
    import sqlalchemy as sa

    async def _insert() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO team_configs "
                    "(repo, coordinator_label, evaluator_label) "
                    "VALUES (:repo, :coord, :evaluator)"
                ),
                {
                    "repo": repo,
                    "coord": f"coordinator-{repo.split('/')[-1]}",
                    "evaluator": f"evaluator-{repo.split('/')[-1]}",
                },
            )

    asyncio.run(_insert())


def test_minimal_doc_submits_end_to_end(client) -> None:
    """Task 56c0b353: POST /plans with a doc omitting both deprecated
    fields returns 201 and spawns the task — the full parse → persist →
    spawn path, executed for real (this test FAILS on the pre-#327
    parser with `workflow: Field required`; control run in the PR)."""
    repo = f"test/minimal-{uuid.uuid4().hex[:8]}"
    client.seed_team_config(repo)

    response = client.post(
        "/api/v1/plans",
        json={
            "repo": repo,
            "doc_path": "docs/plans/2026-06-12-minimal.md",
            "doc_content": _MINIMAL_DOC,
        },
    )

    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["repo"] == repo

    tasks_resp = client.get(f"/api/v1/plans/{plan['id']}/tasks")
    assert tasks_resp.status_code == 200, tasks_resp.text
    tasks = tasks_resp.json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Minimal modern task"


def test_legacy_doc_with_fields_still_submits(client) -> None:
    """Back-compat control surface: a doc WITH the deprecated fields
    keeps submitting exactly as before."""
    repo = f"test/legacy-{uuid.uuid4().hex[:8]}"
    client.seed_team_config(repo)
    legacy_doc = _MINIMAL_DOC.replace(
        "    intent: Post-Phase-5 doc with neither workflow nor validation.",
        "    workflow: wf-author\n"
        "    intent: Legacy doc carrying both deprecated fields.\n"
        "    validation:\n"
        "      - kind: deterministic\n"
        "        description: tests pass\n"
        "        script: pytest -q",
    )

    response = client.post(
        "/api/v1/plans",
        json={
            "repo": repo,
            "doc_path": "docs/plans/2026-06-12-legacy.md",
            "doc_content": legacy_doc,
        },
    )

    assert response.status_code == 201, response.text
    tasks_resp = client.get(f"/api/v1/plans/{response.json()['id']}/tasks")
    assert len(tasks_resp.json()) == 1
