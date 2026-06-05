"""Add repo_configs git author override columns for per-repo identity (ADR-0076).

Three new nullable columns allow operators to override the default git author
identity and commit trailer per repo:
- git_author_name: VARCHAR(255) NULL
- git_author_email: VARCHAR(255) NULL
- commit_trailer: TEXT NULL

A CHECK constraint enforces that name and email are paired: either both are NULL
(use defaults) or both are NOT NULL (use the override pair).

Revision ID: 20260605_1800
Revises: 20260605_1700
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260605_1800"
down_revision: Union[str, Sequence[str], None] = "20260605_1700"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repo_configs",
        sa.Column("git_author_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "repo_configs",
        sa.Column("git_author_email", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "repo_configs",
        sa.Column("commit_trailer", sa.Text, nullable=True),
    )
    op.create_check_constraint(
        "ck_repo_configs_git_author_paired",
        "repo_configs",
        "(git_author_name IS NULL) = (git_author_email IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_repo_configs_git_author_paired",
        "repo_configs",
        type_="check",
    )
    op.drop_column("repo_configs", "commit_trailer")
    op.drop_column("repo_configs", "git_author_email")
    op.drop_column("repo_configs", "git_author_name")
