"""Integration tests for ``seed_starters_if_empty`` (ADR-0028 Q28.a).

Verifies the auto-seed path that runs at API startup:

  1. Empty DB → populates roles + role_versions(v1) + workflows +
     workflow_versions + workflow_version_steps + event_triggers.
  2. Non-empty DB → no-op (returns 0; idempotent across replica
     startups).
  3. The seeded data matches the canonical starters constants.

Skipped by default; requires live Postgres. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest tests/test_integration_auto_seed.py
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = sa.create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str) -> None:
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


_TEST_TABLES = (
    "events",
    "workflow_run_steps",
    "workflow_runs",
    "task_prs",
    "task_dependencies",
    "tasks",
    "plans",
    "workflow_version_steps",
    "workflow_versions",
    "workflows",
    "role_versions",
    "role_skills",
    "role_hooks",
    "skills",
    "hooks",
    "roles",
    "event_triggers",
)


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    """Truncate the world before AND after each test so the auto-seed
    starts against a fresh state."""
    def _do() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(_TEST_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )
    _do()
    yield
    _do()


def _make_session(engine: Engine) -> Session:
    return sessionmaker(bind=engine)()


def test_auto_seed_populates_fresh_db(
    engine: Engine, truncate: None,
) -> None:
    from treadmill_api.starters import (
        STARTERS,
        _DEFAULT_EVENT_TRIGGERS,
        _all_roles,
        seed_starters_if_empty,
    )

    with _make_session(engine) as session:
        seeded = seed_starters_if_empty(session)

    assert seeded == len(_all_roles())

    with engine.connect() as conn:
        role_count = conn.execute(
            sa.text("SELECT count(*) FROM roles")
        ).scalar_one()
        wf_count = conn.execute(
            sa.text("SELECT count(*) FROM workflows")
        ).scalar_one()
        wv_count = conn.execute(
            sa.text("SELECT count(*) FROM workflow_versions")
        ).scalar_one()
        rv_count = conn.execute(
            sa.text("SELECT count(*) FROM role_versions")
        ).scalar_one()
        et_count = conn.execute(
            sa.text("SELECT count(*) FROM event_triggers")
        ).scalar_one()

    assert role_count == len(_all_roles())
    assert wf_count == len(STARTERS)
    assert wv_count == len(STARTERS)  # one v1 per workflow
    # One role_versions row per role at version 1 (the audit-trail
    # baseline so the audit log starts coherent).
    assert rv_count == len(_all_roles())
    assert et_count == len(_DEFAULT_EVENT_TRIGGERS)


def test_auto_seed_on_populated_db_is_noop(
    engine: Engine, truncate: None,
) -> None:
    """A second call against an already-seeded DB returns 0 and
    leaves the row counts unchanged. This is the multi-replica
    startup case: replica 2 wakes up after replica 1 commits, sees
    roles already populated, and proceeds."""
    from treadmill_api.starters import _all_roles, seed_starters_if_empty

    with _make_session(engine) as session:
        seed_starters_if_empty(session)  # first call seeds
    with _make_session(engine) as session:
        seeded = seed_starters_if_empty(session)  # second is a no-op

    assert seeded == 0

    with engine.connect() as conn:
        role_count = conn.execute(
            sa.text("SELECT count(*) FROM roles")
        ).scalar_one()
        rv_count = conn.execute(
            sa.text("SELECT count(*) FROM role_versions")
        ).scalar_one()
    # No duplicates: same counts as after the first seed.
    assert role_count == len(_all_roles())
    assert rv_count == len(_all_roles())


def test_auto_seed_role_versions_have_v1_baseline(
    engine: Engine, truncate: None,
) -> None:
    """Every seeded role should have a single role_versions row at
    version=1 with notes recording the auto-seed origin. This makes
    the audit trail coherent from the start."""
    from treadmill_api.starters import _all_roles, seed_starters_if_empty

    with _make_session(engine) as session:
        seed_starters_if_empty(session)

    role_ids = [r["id"] for r in _all_roles()]
    with engine.connect() as conn:
        for role_id in role_ids:
            rows = conn.execute(
                sa.text(
                    "SELECT version, notes, created_by FROM role_versions "
                    "WHERE role_id = :rid ORDER BY version"
                ),
                {"rid": role_id},
            ).all()
            assert len(rows) == 1, f"expected exactly 1 v1 row for {role_id}"
            assert rows[0].version == 1
            assert "auto-seed" in (rows[0].notes or "")
            assert rows[0].created_by == "auto-seed"
