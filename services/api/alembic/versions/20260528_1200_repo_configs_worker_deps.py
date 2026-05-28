"""Add repo_configs.worker_deps_{python,node} + repo_worker_binaries (ADR-0059).

Step 1 of the ADR-0059 per-repo worker-dep registration: extends the
ADR-0050 ``repo_configs`` row with two TEXT[] columns for Python / Node
package specs and adds a ``repo_worker_binaries`` side table for
downloaded CLI binaries (one row per binary, FK to ``repo_configs.id``
with ``ON DELETE CASCADE``). Apt / OS-package support is intentionally
deferred to a follow-up — the v1 scope is Python + Node + binaries,
which covers the RAMJAC class of failure that motivated the ADR.

The two array columns are ``NOT NULL`` with ``server_default '{}'`` so
the migration is additive over existing onboarded repos (every existing
row gets empty lists without backfill).

Revision ID: 20260528_1200
Revises: 20260526_1600
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260528_1200"
down_revision: Union[str, Sequence[str], None] = "20260526_1600"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repo_configs",
        sa.Column(
            "worker_deps_python",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "repo_configs",
        sa.Column(
            "worker_deps_node",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_table(
        "repo_worker_binaries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "repo_config_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("repo_configs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("download_url", sa.Text(), nullable=False),
        sa.Column("sha256_checksum", sa.String(64), nullable=False),
        sa.Column("target_path", sa.String(512), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_repo_worker_binaries_repo_config_id",
        "repo_worker_binaries",
        ["repo_config_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_repo_worker_binaries_repo_config_id",
        table_name="repo_worker_binaries",
    )
    op.drop_table("repo_worker_binaries")
    op.drop_column("repo_configs", "worker_deps_node")
    op.drop_column("repo_configs", "worker_deps_python")
