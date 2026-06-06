"""Add repo_configs.sensitive_strings + is_public for the ADR-0078 secret-leak gate.

Two new nullable columns:

- ``is_public`` (BOOLEAN NOT NULL DEFAULT false) — marks a repo as
  publicly visible on GitHub. The Treadmill repo itself is public; most
  ADAPT-mode repos onboarded today are not. The secret-leak gate fires
  only for repos where ``is_public = true``.
- ``sensitive_strings`` (JSONB NULL) — additional substrings the gate
  treats as blockers in vault-side content. Null means "no extra
  patterns beyond the hardcoded baseline" (which the gate ships with).

The Treadmill repo row gets seeded by the matching gate code, not in
this migration — seeding belongs in the secret-leak-gate module so
the schema migration stays purely about shape.

Revision ID: 20260606_0000
Revises: 20260605_1900
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260606_0000"
down_revision: Union[str, Sequence[str], None] = "20260605_1900"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repo_configs",
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "repo_configs",
        sa.Column(
            "sensitive_strings",
            postgresql.JSONB,
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_configs", "sensitive_strings")
    op.drop_column("repo_configs", "is_public")
