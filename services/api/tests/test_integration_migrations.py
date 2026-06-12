"""Integration tests for the migration suite against live Postgres.

Skipped by default. To run:

  TREADMILL_INTEGRATION=1 \
  TREADMILL_TEST_DATABASE_URL=postgresql+psycopg://.../<DEDICATED TEST DB> \
  uv run pytest services/api/tests/test_integration_migrations.py

These tests assume the target Postgres server is up. The URL comes from
TREADMILL_TEST_DATABASE_URL — point it at a DEDICATED test database
(task 9200ef54: integration tests carry no live-DB default).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason="set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL (a DEDICATED test database) to run; requires `treadmill-local up`",
)


@pytest.fixture(scope="module")
def database_url() -> str:
    return TEST_DB_URL


@pytest.fixture(scope="module")
def engine(database_url: str) -> Engine:
    """Sync engine for the integration tests. Migrations run sync via alembic."""
    eng = sa.create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str) -> None:
    """Apply migrations to the database before the test module runs.

    Migrations are idempotent (alembic upgrade head no-ops if already at
    head), so this is safe to run repeatedly. We pass DATABASE_URL via env
    so alembic env.py picks it up; we use the sync (psycopg) URL directly
    because alembic env.py also rewrites +asyncpg → +psycopg.
    """
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


# Current (post-ADR-0087 Phase 5 + ADR-0089) schema. The pre-Phase-5
# workflow/role/validation tables were dropped by migrations
# 20260611_0100/0200; this set was updated for task 9200ef54 (it had
# pinned the dropped tables, so the test was permanently red).
_EXPECTED_TABLES = {
    "plans",
    "tasks",
    "task_prs",
    "task_dependencies",
    "task_executions",
    "task_board",
    "team_configs",
    "repo_configs",
    "repo_profiles",
    "repo_context_docs",
    "repo_worker_binaries",
    "schedules",
    "system_status",
    "llm_calls",
    "llm_harvest_cursors",
    "events",
    "alembic_version",
}


def test_all_expected_tables_exist(engine: Engine) -> None:
    inspector = sa.inspect(engine)
    actual = set(inspector.get_table_names())
    missing = _EXPECTED_TABLES - actual
    assert not missing, f"missing tables: {missing}"


def test_alembic_version_is_at_head(engine: Engine) -> None:
    """The alembic_version table records the head revision after upgrade."""
    with engine.connect() as conn:
        result = conn.execute(sa.text("SELECT version_num FROM alembic_version")).all()
    assert len(result) == 1, "alembic_version table should have exactly one row"


# test_tasks_workflow_version_id_fk_targets_workflow_versions was
# deleted (task 9200ef54): ADR-0087 Phase 5 INVERTED that contract —
# tasks no longer pin a workflow version, and the unit test
# test_task_has_no_workflow_version_pin pins the column's ABSENCE.


def test_tasks_parent_task_id_self_fk_exists(engine: Engine) -> None:
    """Per ADR-0048, ``tasks.parent_task_id`` is a self-FK linking child
    → parent for supersede lineage. The migration adds the column,
    self-FK with ON DELETE SET NULL, and a backing index."""
    inspector = sa.inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("tasks")}
    assert "parent_task_id" in cols, (
        "migration 20260519_1718 must add parent_task_id to tasks"
    )
    assert cols["parent_task_id"]["nullable"] is True

    # Self-FK.
    fks = inspector.get_foreign_keys("tasks")
    self_fks = [
        fk for fk in fks
        if fk["referred_table"] == "tasks"
        and tuple(fk["constrained_columns"]) == ("parent_task_id",)
    ]
    assert len(self_fks) == 1
    assert self_fks[0]["options"].get("ondelete", "").upper() == "SET NULL"

    # Backing index.
    indexes = inspector.get_indexes("tasks")
    by_name = {ix["name"]: ix for ix in indexes}
    assert "ix_tasks_parent_task_id" in by_name
    assert by_name["ix_tasks_parent_task_id"]["column_names"] == ["parent_task_id"]


def test_task_prs_uses_composite_primary_key(engine: Engine) -> None:
    """The (repo, pr_number) PK matches the bunkhouse pattern per ADR-0007."""
    inspector = sa.inspect(engine)
    pk = inspector.get_pk_constraint("task_prs")
    assert set(pk["constrained_columns"]) == {"repo", "pr_number"}


def test_events_payload_is_jsonb(engine: Engine) -> None:
    """The single allowed site for JSONB on events. Confirms the column
    type made it through the migration intact."""
    inspector = sa.inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("events")}
    payload_type = str(cols["payload"]["type"]).upper()
    assert "JSON" in payload_type  # JSONB renders as JSONB in inspector dump


def test_events_commit_sha_column_exists(engine: Engine) -> None:
    """Migration 0005 adds the ``commit_sha`` TEXT column to events per
    ADR-0014. Nullable so pre-commit events (``plan.registered`` etc.)
    can still write."""
    inspector = sa.inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("events")}
    assert "commit_sha" in cols, "0005 must add commit_sha to events"
    commit_sha = cols["commit_sha"]
    assert "TEXT" in str(commit_sha["type"]).upper()
    assert commit_sha["nullable"] is True


def test_events_commit_sha_partial_indexes_exist(engine: Engine) -> None:
    """The two ADR-0014 partial indexes — both must filter ``commit_sha
    IS NOT NULL`` so the index stays small over the NULL majority of
    pre-commit events. Verified via ``pg_indexes`` because SQLAlchemy's
    introspection doesn't expose partial-WHERE clauses cleanly."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = 'events'"
            )
        ).all()
    by_name = {r.indexname: r.indexdef for r in rows}

    assert "ix_events_task_commit" in by_name
    task_idx = by_name["ix_events_task_commit"]
    assert "(task_id, commit_sha)" in task_idx
    assert "commit_sha IS NOT NULL" in task_idx

    assert "ix_events_entity_action_commit" in by_name
    eac_idx = by_name["ix_events_entity_action_commit"]
    assert "(entity_type, action, commit_sha)" in eac_idx
    assert "commit_sha IS NOT NULL" in eac_idx


# test_task_validations_columns_match_model and
# test_migration_0007_seeds_event_triggers_when_workflows_present were
# deleted (task 9200ef54): the task_validations and event_triggers/
# workflows tables they pinned were dropped by ADR-0087 Phase 5, so
# both tests errored unconditionally on a current schema.


def test_inserting_a_plan_row_works(engine: Engine) -> None:
    """End-to-end smoke: insert a plan, read it back, delete it."""
    with engine.begin() as conn:
        result = conn.execute(
            sa.text(
                "INSERT INTO plans (repo, intent, created_by) "
                "VALUES (:repo, :intent, :by) RETURNING id"
            ),
            {"repo": "test/test-repo", "intent": "test plan", "by": "pytest"},
        )
        plan_id = result.scalar()
        assert plan_id is not None

    with engine.begin() as conn:
        row = conn.execute(
            sa.text("SELECT repo, intent FROM plans WHERE id = :id"),
            {"id": plan_id},
        ).one()
        assert row.repo == "test/test-repo"
        assert row.intent == "test plan"

        conn.execute(sa.text("DELETE FROM plans WHERE id = :id"), {"id": plan_id})
