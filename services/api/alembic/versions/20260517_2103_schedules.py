"""schedules table (ADR-0035 §Decision).

Adds the ``schedules`` table that drives periodic workflow dispatches.
Each row carries a cron expression, a workflow binding, quiet-hour
configuration, and jitter settings following RAMJAC's scrape-scheduler
design (see ADR-0035 §References).

``payload_template`` is JSONB — the third explicit JSONB site in
Treadmill (ADR-0011 exception granted by ADR-0035).

Revision ID: 20260517_2103
Revises: 0017

Renamed from 0017_schedules.py on 2026-05-17 after the revision-0017 slot
collided with 0017_task_status_surface_decision_fail.py. Both migrations
merged separately to main, each passing its own author-side validation
against a fresh DB; the collision only manifested at runtime after both
landed (alembic warned "Revision 0017 is present more than once" and
silently picked one to apply, leaving this migration orphaned and the
schedules table un-created — the entire ADR-0035 scheduler plan was
non-functional until this rename).

This migration is the first to adopt datetime-keyed revision IDs per
ADR-0044 (proposed). The chain now mixes formats: existing sequential
IDs through 0017, then datetime IDs going forward. Future migrations
follow ``revision="YYYYMMDD_HHMM"``; ``down_revision`` continues to
reference whatever the previous head was, sequential or datetime.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260517_2103"
down_revision: Union[str, Sequence[str], None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("cron_expression", sa.String(length=128), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column(
            "payload_template",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "jitter_seconds",
            sa.Integer(),
            server_default=sa.text("60"),
            nullable=False,
        ),
        sa.Column("quiet_hours", sa.String(length=16), nullable=True),
        sa.Column(
            "quiet_tz",
            sa.String(length=64),
            server_default=sa.text("'America/Los_Angeles'"),
            nullable=False,
        ),
        sa.Column(
            "quiet_multiplier",
            sa.Float(),
            server_default=sa.text("6.0"),
            nullable=False,
        ),
        sa.Column(
            "quiet_max_seconds",
            sa.Integer(),
            server_default=sa.text("43200"),
            nullable=False,
        ),
        sa.Column(
            "last_fired_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused')",
            name="ck_schedules_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedules_status", "schedules", ["status"], unique=False)
    op.create_index(
        "ix_schedules_workflow_id", "schedules", ["workflow_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_schedules_workflow_id", table_name="schedules")
    op.drop_index("ix_schedules_status", table_name="schedules")
    op.drop_table("schedules")
