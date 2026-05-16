"""Smoke tests for the Schedule model (ADR-0035).

No database required. Integration CRUD tests (upgrade + roundtrip against
live Postgres) live in ``test_integration_local.py``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Float, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

from treadmill_api.models.schedule import Schedule


def test_schedule_table_name():
    assert Schedule.__tablename__ == "schedules"


def test_schedule_column_names():
    cols = {c.name for c in Schedule.__table__.columns}
    assert cols == {
        "id",
        "cron_expression",
        "workflow_id",
        "payload_template",
        "status",
        "jitter_seconds",
        "quiet_hours",
        "quiet_tz",
        "quiet_multiplier",
        "quiet_max_seconds",
        "last_fired_at",
        "created_by",
        "created_at",
    }


def test_schedule_id_is_uuid_pk():
    col = Schedule.__table__.columns["id"]
    assert isinstance(col.type, UUID)
    assert col.primary_key
    assert not col.nullable


def test_schedule_payload_template_is_jsonb():
    col = Schedule.__table__.columns["payload_template"]
    assert isinstance(col.type, JSONB)
    assert not col.nullable
    assert "'{}'::jsonb" in str(col.server_default.arg)


def test_schedule_status_check_constraint():
    check_names = {
        c.name
        for c in Schedule.__table__.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_schedules_status" in check_names


def test_schedule_status_check_covers_valid_values():
    """CHECK constraint must gate on 'active' and 'paused' only."""
    check = next(
        c
        for c in Schedule.__table__.constraints
        if c.__class__.__name__ == "CheckConstraint"
        and c.name == "ck_schedules_status"
    )
    expr = str(check.sqltext)
    assert "active" in expr
    assert "paused" in expr


def test_schedule_status_default_is_active():
    col = Schedule.__table__.columns["status"]
    assert isinstance(col.type, String)
    assert "'active'" in str(col.server_default.arg)


def test_schedule_jitter_seconds_defaults():
    col = Schedule.__table__.columns["jitter_seconds"]
    assert isinstance(col.type, Integer)
    assert not col.nullable
    assert "60" in str(col.server_default.arg)


def test_schedule_quiet_hours_is_nullable():
    col = Schedule.__table__.columns["quiet_hours"]
    assert col.nullable is True


def test_schedule_quiet_tz_default():
    col = Schedule.__table__.columns["quiet_tz"]
    assert not col.nullable
    assert "America/Los_Angeles" in str(col.server_default.arg)


def test_schedule_quiet_multiplier_type_and_default():
    col = Schedule.__table__.columns["quiet_multiplier"]
    assert isinstance(col.type, Float)
    assert not col.nullable
    assert "6.0" in str(col.server_default.arg)


def test_schedule_quiet_max_seconds_default():
    col = Schedule.__table__.columns["quiet_max_seconds"]
    assert isinstance(col.type, Integer)
    assert not col.nullable
    assert "43200" in str(col.server_default.arg)


def test_schedule_last_fired_at_is_nullable_timestamptz():
    col = Schedule.__table__.columns["last_fired_at"]
    assert col.nullable is True
    assert isinstance(col.type, TIMESTAMP)
    assert col.type.timezone is True


def test_schedule_created_at_is_timestamptz_not_null():
    col = Schedule.__table__.columns["created_at"]
    assert isinstance(col.type, TIMESTAMP)
    assert col.type.timezone is True
    assert not col.nullable
    assert "now()" in str(col.server_default.arg)


def test_schedule_created_by_is_not_null():
    col = Schedule.__table__.columns["created_by"]
    assert isinstance(col.type, String)
    assert not col.nullable


def test_schedule_indexes():
    index_names = {idx.name for idx in Schedule.__table__.indexes}
    assert "ix_schedules_status" in index_names
    assert "ix_schedules_workflow_id" in index_names


def test_schedule_model_instantiation():
    """Schedule instances can be created without hitting the DB (CRUD smoke)."""
    s = Schedule(
        cron_expression="0 * * * *",
        workflow_id="wf-learnings-crystallize",
        payload_template={},
        created_by="operator",
    )
    assert s.cron_expression == "0 * * * *"
    assert s.workflow_id == "wf-learnings-crystallize"
    assert s.payload_template == {}
    assert s.created_by == "operator"


def test_schedule_status_enum_accepts_active():
    s = Schedule(
        cron_expression="0 9 * * 1",
        workflow_id="wf-doc-audit",
        payload_template={},
        created_by="operator",
        status="active",
    )
    assert s.status == "active"


def test_schedule_status_enum_accepts_paused():
    s = Schedule(
        cron_expression="0 9 * * 1",
        workflow_id="wf-doc-audit",
        payload_template={},
        created_by="operator",
        status="paused",
    )
    assert s.status == "paused"
