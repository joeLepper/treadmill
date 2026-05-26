"""Smoke tests for the SQLAlchemy models — importability + schema shape.

These are unit tests; no database required. The integration test for the
migration (upgrade + downgrade against live Postgres) lives in
``test_integration_local.py``.
"""

from __future__ import annotations

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

from treadmill_api.database import Base
from treadmill_api.models import (
    Event,
    EventTrigger,
    Hook,
    Plan,
    Role,
    RoleHook,
    RoleSkill,
    Schedule,
    Skill,
    Task,
    TaskDependency,
    TaskPR,
    TaskValidation,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
    WorkflowVersionStep,
)


def test_all_models_register_on_base_metadata():
    """Importing the models package adds every table to Base.metadata."""
    expected = {
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
        "schedules",
    }
    actual = set(Base.metadata.tables.keys())
    missing = expected - actual
    assert not missing, f"missing tables in Base.metadata: {missing}"


def test_task_validation_table_shape():
    """``task_validations`` carries the ``validation:`` block from a
    plan-doc task spec. The model must expose every column the spec
    promises (per the 2026-05-11 closure plan D.3) plus the integrity
    constraints that keep it honest."""
    table = TaskValidation.__table__
    cols = {c.name for c in table.columns}
    assert cols == {
        "id",
        "task_id",
        "position",
        "kind",
        "description",
        "script",
        "prompt",
        "created_at",
    }

    # task_id FK targets tasks.id with cascade.
    fk = next(iter(TaskValidation.__table__.foreign_keys))
    assert fk.column.table.name == "tasks"
    assert fk.ondelete == "CASCADE"

    # CHECK constraint enforces script/prompt pairing per kind.
    check_names = {
        c.name for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_task_validations_kind_script_prompt" in check_names

    # UNIQUE (task_id, position) so re-renders stay stable.
    uniques = [
        c for c in table.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert any(
        {col.name for col in c.columns} == {"task_id", "position"}
        for c in uniques
    )


def test_task_has_workflow_version_id_fk():
    """Tasks pin to a specific workflow version per ADR-0010."""
    fkeys = {fk.column.table.name for fk in Task.__table__.foreign_keys}
    assert "workflow_versions" in fkeys
    assert "plans" in fkeys


def test_task_pr_has_composite_primary_key():
    """The (repo, pr_number) → task_id bridge per ADR-0007 uses a
    composite PK so webhook lookups don't scan."""
    pk_cols = {c.name for c in TaskPR.__table__.primary_key.columns}
    assert pk_cols == {"repo", "pr_number"}


def test_event_payload_is_jsonb():
    """events.payload is JSONB per ADR-0011 (one of two allowed sites)."""
    payload_col = Event.__table__.columns["payload"]
    assert isinstance(payload_col.type, JSONB)


def test_event_commit_sha_is_nullable_text():
    """events.commit_sha is the ADR-0014 column. Nullable TEXT — populated
    by the webhook receiver / consumer / dispatcher when the event runs
    against (or describes) a specific HEAD; NULL for pre-commit events.
    """
    table = Event.__table__
    assert "commit_sha" in table.columns, (
        "Event model must declare commit_sha per ADR-0014"
    )
    col = table.columns["commit_sha"]
    assert isinstance(col.type, Text)
    assert col.nullable is True


def test_event_has_partial_indexes_on_commit_sha():
    """ADR-0014 partial indexes — both must declare a ``commit_sha IS NOT
    NULL`` predicate so the index stays small over the NULL majority."""
    indexes = {idx.name: idx for idx in Event.__table__.indexes}
    assert "ix_events_task_commit" in indexes
    assert "ix_events_entity_action_commit" in indexes
    for name in ("ix_events_task_commit", "ix_events_entity_action_commit"):
        idx = indexes[name]
        where = idx.dialect_options["postgresql"].get("where")
        assert where is not None, f"{name} must have a partial-index WHERE"
        assert "commit_sha IS NOT NULL" in str(where), (
            f"{name}.where must filter commit_sha IS NOT NULL"
        )
    assert [c.name for c in indexes["ix_events_task_commit"].columns] == [
        "task_id", "commit_sha",
    ]
    assert [c.name for c in indexes["ix_events_entity_action_commit"].columns] == [
        "entity_type", "action", "commit_sha",
    ]


def test_workflow_run_step_output_is_jsonb():
    """workflow_run_steps.output is JSONB per ADR-0011 (the second
    allowed site)."""
    output_col = WorkflowRunStep.__table__.columns["output"]
    assert isinstance(output_col.type, JSONB)


def test_workflow_run_step_has_token_usage_columns():
    """ADR-0020 Wave 1: per-step token usage telemetry is persisted via
    five dedicated columns. All must be nullable — validation steps,
    dry-run paths, and historical rows from before Wave 1 leave them
    NULL."""
    from sqlalchemy import BigInteger

    cols = WorkflowRunStep.__table__.columns
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
    ):
        assert name in cols, f"{name} column missing on workflow_run_steps"
        assert cols[name].nullable, f"{name} must be nullable"
        # BigInteger so a long-running deployment doesn't bump into INT32
        # rollover on aggregate rollups.
        assert isinstance(cols[name].type, BigInteger), (
            f"{name} must be BigInteger so aggregates don't overflow"
        )
    assert "model" in cols
    assert cols["model"].nullable
    assert isinstance(cols["model"].type, Text)


def test_no_unexpected_jsonb_columns():
    """ADR-0011 restricts JSONB to explicit allowed sites only.

    Allowed sites:
    - events.payload (ADR-0011)
    - workflow_run_steps.output (ADR-0011)
    - schedules.payload_template (ADR-0035 exception)
    """
    allowed = {
        ("events", "payload"),
        ("workflow_run_steps", "output"),
        ("schedules", "payload_template"),
    }
    found = {
        (table.name, col.name)
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, JSONB)
    }
    assert found == allowed, (
        f"unexpected JSONB columns: {found - allowed}; "
        f"missing expected: {allowed - found}"
    )


def test_uuid_primary_keys_use_postgres_uuid_type():
    """UUID PKs use the Postgres UUID type with as_uuid=True so the model
    layer hands real ``uuid.UUID`` objects, not strings."""
    for model in (Plan, Task, TaskDependency, WorkflowVersion, WorkflowRun, WorkflowRunStep, Event, EventTrigger, Schedule):
        col = model.__table__.columns["id"]
        assert isinstance(col.type, UUID), f"{model.__name__}.id is not UUID"


def test_timestamps_are_timezone_aware():
    """Every created_at column uses TIMESTAMPTZ — never naive."""
    for table in Base.metadata.tables.values():
        if "created_at" in table.columns:
            col = table.columns["created_at"]
            assert isinstance(col.type, TIMESTAMP) and col.type.timezone, (
                f"{table.name}.created_at must be TIMESTAMPTZ"
            )


def test_string_id_models_use_slug_pks():
    """Workflow / Role / Skill / Hook are slug-keyed (string PK)."""
    for model in (Workflow, Role, Skill, Hook):
        col = model.__table__.columns["id"]
        assert col.type.python_type is str
        assert col.primary_key
