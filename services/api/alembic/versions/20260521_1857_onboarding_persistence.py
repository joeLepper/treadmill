"""Onboarding persistence — repo_configs, repo_profiles, repo_context_docs (ADR-0050).

Persists the ADR-0050 onboarding shapes already defined in
``treadmill_api/repo_config.py`` (RepoConfig) and
``treadmill_api/repo_profile.py`` (RepoProfile), plus an index of S3-backed
context documents per (repo, doc_path).

Per ADR-0011, repo metadata uses TYPED COLUMNS — no JSONB. List-valued
fields (languages, doc_paths, components) use Postgres ``ARRAY(String)``
with an empty-array server default.

Three tables:

  * ``repo_configs`` — per-repo onboarding config (mode + auto-merge block
    + discovered test/lint commands). UNIQUE on ``repo`` since each repo
    holds exactly one config row.
  * ``repo_profiles`` — wf-discover's structured output (languages,
    build/test/lint, doc layout, CI, agent-context flag). UNIQUE on
    ``repo``; re-discovering a repo updates this row.
  * ``repo_context_docs`` — index of context docs the onboarding flow has
    persisted to the S3 content store. ``(repo, doc_path, version)`` is
    UNIQUE; ``record_context_doc`` writes the next version monotonically,
    so old versions stay queryable and the highest version is the current
    pointer.

Revision ID: 20260521_1857
Revises: 20260520_0500
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260521_1857"
down_revision: Union[str, Sequence[str], None] = "20260520_0500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repo_configs",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column(
            "mode",
            sa.String(length=16),
            server_default=sa.text("'conform'"),
            nullable=False,
        ),
        sa.Column(
            "auto_merge_blocked",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("test_command", sa.String(), nullable=True),
        sa.Column("lint_command", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo", name="uq_repo_configs_repo"),
    )

    op.create_table(
        "repo_profiles",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column(
            "languages",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("build_command", sa.String(), nullable=True),
        sa.Column("test_command", sa.String(), nullable=True),
        sa.Column("lint_command", sa.String(), nullable=True),
        sa.Column(
            "doc_paths",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "components",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("ci", sa.String(), nullable=True),
        sa.Column(
            "has_agent_context",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo", name="uq_repo_profiles_repo"),
    )

    op.create_table(
        "repo_context_docs",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("doc_path", sa.String(), nullable=False),
        sa.Column("s3_key", sa.String(), nullable=False),
        sa.Column("content_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repo",
            "doc_path",
            "version",
            name="uq_repo_context_docs_repo_doc_version",
        ),
    )
    op.create_index(
        "ix_repo_context_docs_repo_doc",
        "repo_context_docs",
        ["repo", "doc_path"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_repo_context_docs_repo_doc", table_name="repo_context_docs"
    )
    op.drop_table("repo_context_docs")
    op.drop_table("repo_profiles")
    op.drop_table("repo_configs")
