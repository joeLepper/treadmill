"""Smoke tests for the SQLAlchemy models — importability + schema shape.

These are unit tests; no database required. The integration test for the
migration (upgrade + downgrade against live Postgres) lives in
``test_integration_local.py``.

Post-ADR-0087 Phase 5 the model surface is the lean set: plans, tasks
(+ deps + PRs), task_executions + llm_calls, events, schedules,
team_configs, onboarding rows, task_board, system_status. The workflow
definition/run layer, roles/skills/hooks, task_validations, and the
DSPy corpora are gone with their tables.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

from treadmill_api.database import Base
from treadmill_api.models import (
    Event,
    LLMCall,
    Plan,
    Schedule,
    Task,
    TaskDependency,
    TaskExecution,
    TaskPR,
)


def test_all_models_register_on_base_metadata():
    """Importing the models package adds every table to Base.metadata."""
    expected = {
        "plans",
        "tasks",
        "task_prs",
        "task_dependencies",
        "task_executions",
        "llm_calls",
        "events",
        "schedules",
        "team_configs",
    }
    actual = set(Base.metadata.tables.keys())
    missing = expected - actual
    assert not missing, f"missing tables in Base.metadata: {missing}"


def test_no_legacy_execution_tables_registered():
    """ADR-0087 Phases 4–5 dropped the legacy execution model. None of
    its tables may re-enter Base.metadata — a stray import of a deleted
    model module would silently re-register one and resurrect it on the
    next autogenerate."""
    legacy = {
        "workflows",
        "workflow_versions",
        "workflow_version_steps",
        "workflow_runs",
        "workflow_run_steps",
        "workflow_dispatch_dedup",
        "roles",
        "role_versions",
        "role_skills",
        "role_hooks",
        "skills",
        "hooks",
        "event_triggers",
        "task_validations",
        "architect_gold_rows",
        "validator_gold_rows",
        "review_dspy_variant_pr",
        "triage_findings",
    }
    present = legacy & set(Base.metadata.tables.keys())
    assert not present, f"legacy tables re-registered: {present}"


def test_task_has_no_workflow_version_pin():
    """ADR-0087 Phase 5 — tasks no longer pin a workflow version; the
    coordinator decides execution at dispatch time."""
    assert "workflow_version_id" not in Task.__table__.columns
    fkeys = {fk.column.table.name for fk in Task.__table__.foreign_keys}
    assert "plans" in fkeys
    assert "workflow_versions" not in fkeys


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
    by the webhook receiver / dispatcher when the event runs against (or
    describes) a specific HEAD; NULL for pre-commit events.
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


def test_llm_call_has_token_usage_columns():
    """ADR-0087 — ``llm_calls`` carries the per-subprocess token
    attribution. Counter columns are ``BIGINT`` (aggregates over a
    long-lived team can blow past ``int4`` headroom); cache columns are
    nullable (not every call reports cache usage)."""
    table = LLMCall.__table__
    for name in ("input_tokens", "output_tokens"):
        col = table.columns[name]
        assert isinstance(col.type, BigInteger)
        assert col.nullable is False, f"{name} must be NOT NULL"
    for name in ("cache_creation_tokens", "cache_read_tokens"):
        col = table.columns[name]
        assert isinstance(col.type, BigInteger)
        assert col.nullable is True, f"{name} must be nullable"
    assert isinstance(table.columns["model"].type, Text)


def test_task_execution_shape():
    """ADR-0087 — ``task_executions`` is the coordinator's lifecycle
    write target. The 409/CHECK behaviors are pinned in
    ``test_routers_task_executions``; here we pin the column shape."""
    table = TaskExecution.__table__
    for name in ("task_id", "worker_label", "trigger", "status",
                 "started_at"):
        assert name in table.columns, f"missing column {name!r}"
    assert table.columns["failure_reason"].nullable is True
    assert table.columns["completed_at"].nullable is True


def test_no_unexpected_jsonb_columns():
    """ADR-0011 restricts JSONB to explicit allowed sites only.

    Allowed sites:
    - events.payload (ADR-0011)
    - schedules.payload_template (ADR-0035 exception)
    - repo_configs.sensitive_strings (ADR-0078 — operator-curated
      list of additional substrings the secret-leak gate blocks on
      vault writes)

    ``workflow_run_steps.output`` and ``triage_findings.evidence_summary``
    left the list with their tables (ADR-0087 Phases 4–5).
    """
    allowed = {
        ("events", "payload"),
        ("schedules", "payload_template"),
        ("repo_configs", "sensitive_strings"),
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
    for model in (Plan, Task, TaskDependency, TaskExecution, LLMCall,
                  Event, Schedule):
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


def test_mergeability_view_migration_projects_detail_columns():
    """Regression for the Phase 5 hotfix (20260611_0300): the
    ``task_mergeability`` VIEW must project the five detail columns its
    live consumers SELECT (``GET /tasks/{id}/mergeability`` +
    dashboard overview) alongside ``derived_mergeability``. The Phase 4
    rewrite silently narrowed the projection and both consumers 500'd
    with UndefinedColumnError on deploy."""
    from pathlib import Path as _P

    body = (
        _P(__file__).resolve().parent.parent
        / "alembic" / "versions"
        / "20260611_0300_mergeability_detail_columns.py"
    ).read_text()
    for col in (
        "head.head_sha",
        "review.decision AS review_decision",
        "validate.decision AS validate_decision",
        "ci.conclusion AS ci_conclusion",
        "conflict.is_conflicting AS pr_conflicting",
    ):
        assert col in body, f"VIEW projection missing {col!r}"


def test_metadata_sorted_tables_resolves_all_foreign_keys():
    """Force SQLAlchemy to resolve every ORM-declared ForeignKey against
    Base.metadata. A string-based ForeignKey pointing at a deleted
    model's table raises NoReferencedTableError here — the same error
    that otherwise first fires at INSERT-flush time in production
    (2026-06-10: every events INSERT 500'd because Event.run_id /
    .step_id still declared FKs to the dropped workflow_runs tables)."""
    tables = Base.metadata.sorted_tables
    assert len(tables) >= 9
