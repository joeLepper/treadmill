"""role_versions audit table per ADR-0028.

ADR-0028 (resolved 2026-05-13) flips the source of truth for role
prompts from ``starters.py`` (code) to the DB. The operator's edit
workflow becomes ``treadmill role update <id> --prompt-from-file
<path>`` against the API; each edit appends a new row to
``role_versions``. Q28.d's resolution adds ``notes`` and ``pr_url``
columns so a prompt change can be linked back to its rationale (a
PR, an incident, an experiment).

The live ``roles.system_prompt`` column stays — it's the hot-path
read for workers and the consumer (no JOIN cost). ``role_versions``
is the audit log; the PATCH endpoint writes both in one transaction.

Schema:

  role_versions(
    id          uuid primary key default gen_random_uuid(),
    role_id     varchar(64)  references roles(id) on delete cascade,
    version     integer      not null,
    system_prompt text       not null,
    notes       text         null,
    pr_url      text         null,
    created_at  timestamptz  not null default now(),
    created_by  varchar(128) null,
    unique (role_id, version)
  );
  index ix_role_versions_role_id_version on (role_id, version desc);

Backfill: for each existing role, insert a v1 row capturing the
current prompt. Future PATCHes start at v2. This preserves history
from migration time forward; pre-migration history is lost (it
lived in git instead).

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "role_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "role_id",
            sa.String(length=64),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.UniqueConstraint("role_id", "version", name="uq_role_versions_role_id_version"),
    )
    op.create_index(
        "ix_role_versions_role_id_version",
        "role_versions",
        ["role_id", sa.text("version DESC")],
    )

    # Backfill v1 for every existing role. ``created_by`` is left NULL
    # (the row predates the audit trail); ``notes`` records the
    # backfill so a later reader can distinguish "history from the
    # bootstrap" from "history from operator edits".
    op.execute(
        sa.text(
            "INSERT INTO role_versions "
            "(role_id, version, system_prompt, notes, created_by) "
            "SELECT id, 1, system_prompt, "
            "'backfilled at alembic 0010 from roles.system_prompt', "
            "'alembic:0010' "
            "FROM roles"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_role_versions_role_id_version", table_name="role_versions")
    op.drop_table("role_versions")
