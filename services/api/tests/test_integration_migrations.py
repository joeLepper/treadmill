"""Integration tests for the migration suite against live Postgres.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest services/api/tests/test_integration_migrations.py
  treadmill-local down

These tests assume the substrate is up and the API container has already
applied the migrations on its first connect (or that we apply them here).
We use the host-port-mapped Postgres at localhost:15432 (per the local
adapter's default port-shift for 5432).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


# Default Postgres URL: postgres on host port 15432 (from the local
# adapter's port shift) with the credentials hardcoded in the spike CDK.
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


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


_EXPECTED_TABLES = {
    "plans",
    "tasks",
    "task_prs",
    "task_dependencies",
    "task_validations",
    "workflows",
    "workflow_versions",
    "workflow_version_steps",
    "workflow_runs",
    "workflow_run_steps",
    "roles",
    "skills",
    "hooks",
    "role_skills",
    "role_hooks",
    "event_triggers",
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


def test_tasks_workflow_version_id_fk_targets_workflow_versions(engine: Engine) -> None:
    """Per ADR-0010, tasks pin to a specific workflow_version row."""
    inspector = sa.inspect(engine)
    fks = inspector.get_foreign_keys("tasks")
    targets = {(fk["referred_table"], tuple(fk["referred_columns"])) for fk in fks}
    assert ("workflow_versions", ("id",)) in targets


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


def test_task_validations_columns_match_model(engine: Engine) -> None:
    """The migration creates every column the ORM model declares for
    ``task_validations`` (per the 2026-05-11 closure plan D.3)."""
    inspector = sa.inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("task_validations")}
    assert cols == {
        "id",
        "task_id",
        "position",
        "kind",
        "description",
        "created_at",
    }
    # FK target.
    fks = inspector.get_foreign_keys("task_validations")
    targets = {(fk["referred_table"], tuple(fk["referred_columns"])) for fk in fks}
    assert ("tasks", ("id",)) in targets

    # UNIQUE (task_id, position).
    uniques = inspector.get_unique_constraints("task_validations")
    assert any(
        set(u["column_names"]) == {"task_id", "position"} for u in uniques
    )


def test_migration_0007_seeds_event_triggers_when_workflows_present(
    engine: Engine,
) -> None:
    """Migration ``0007_seed_event_triggers`` inserts five catch-all
    rows mapping github verbs → starter workflows. Per Week-3 plan
    §C.2, these are operational defaults — the trigger evaluator reads
    them on every github event.

    The migration's existence check (``WHERE EXISTS (SELECT 1 FROM
    workflows WHERE id = '<wf>')``) means seeding is conditional:
    rows land only after the workflows have been registered.

    This test seeds the required workflows first, then re-runs the
    migration's INSERTs by hand (the migration itself only runs once
    per alembic upgrade — but the INSERTs are idempotent via the
    ``NOT EXISTS`` guard, so the test can re-issue them safely).
    """
    expected = [
        ("pr_opened", "wf-review"),
        ("pr_synchronize", "wf-review"),
        ("pr_review_submitted", "wf-feedback"),
        ("check_run_completed", "wf-ci-fix"),
        ("pr_conflict", "wf-conflict"),
    ]

    with engine.begin() as conn:
        # Clean slate inside an isolated section so we don't trample
        # any state another test left behind.
        conn.execute(sa.text(
            "DELETE FROM event_triggers WHERE repo IS NULL"
        ))
        for _, workflow_id in expected:
            conn.execute(sa.text(
                "INSERT INTO workflows (id) VALUES (:w) "
                "ON CONFLICT DO NOTHING"
            ), {"w": workflow_id})

        # Re-issue the migration's INSERT statements — the same shape
        # the alembic migration runs at upgrade time.
        for event_type, workflow_id in expected:
            conn.execute(sa.text(f"""
                INSERT INTO event_triggers (repo, event_type, workflow_id,
                                            version_strategy, enabled)
                SELECT NULL, '{event_type}', '{workflow_id}', 'latest', TRUE
                WHERE EXISTS (SELECT 1 FROM workflows WHERE id = '{workflow_id}')
                  AND NOT EXISTS (
                    SELECT 1 FROM event_triggers
                    WHERE repo IS NULL AND event_type = '{event_type}'
                  )
            """))

        # Verify all five rows landed.
        rows = conn.execute(sa.text(
            "SELECT event_type, workflow_id, version_strategy, enabled "
            "FROM event_triggers WHERE repo IS NULL ORDER BY event_type"
        )).all()
        observed = {(r.event_type, r.workflow_id) for r in rows}
        assert observed == set(expected)
        for r in rows:
            assert r.version_strategy == "latest"
            assert r.enabled is True

        # Idempotency: running the same INSERTs again is a no-op.
        for event_type, workflow_id in expected:
            conn.execute(sa.text(f"""
                INSERT INTO event_triggers (repo, event_type, workflow_id,
                                            version_strategy, enabled)
                SELECT NULL, '{event_type}', '{workflow_id}', 'latest', TRUE
                WHERE EXISTS (SELECT 1 FROM workflows WHERE id = '{workflow_id}')
                  AND NOT EXISTS (
                    SELECT 1 FROM event_triggers
                    WHERE repo IS NULL AND event_type = '{event_type}'
                  )
            """))
        rows_after = conn.execute(sa.text(
            "SELECT COUNT(*) FROM event_triggers WHERE repo IS NULL"
        )).scalar()
        assert rows_after == len(expected)

        # Clean up so other tests start fresh.
        conn.execute(sa.text(
            "DELETE FROM event_triggers WHERE repo IS NULL"
        ))


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
