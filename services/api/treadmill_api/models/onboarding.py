"""Onboarding persistence models per ADR-0050.

Three tables back the onboarding flow:

  * ``repo_configs`` — the source-of-truth per-repo onboarding config
    (mode + auto-merge block + discovered commands). One row per repo.
  * ``repo_profiles`` — wf-discover's structured output for a repo
    (languages, build/test/lint, doc layout, CI, agent-context flag).
    One row per repo; re-discovery updates the row.
  * ``repo_context_docs`` — index of context docs the onboarding flow has
    persisted to the S3 content store. ``(repo, doc_path, version)`` is
    UNIQUE; old versions stay queryable and the highest version is the
    current pointer.

Per ADR-0011, list-valued columns use Postgres ``ARRAY(String)`` rather
than JSONB.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class RepoConfigRow(Base):
    """Per-repo onboarding config (ADR-0050 decision 5)."""

    __tablename__ = "repo_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'conform'"),
    )
    auto_merge_blocked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    test_command: Mapped[str | None] = mapped_column(String, nullable=True)
    lint_command: Mapped[str | None] = mapped_column(String, nullable=True)
    # Named Claude account for workers operating on this repo (ADR-0055).
    # ``NULL`` defers to the deployment's ``claude_default_account``.
    claude_account: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint("repo", name="uq_repo_configs_repo"),
    )


class RepoProfileRow(Base):
    """wf-discover's structured output for a repo (ADR-0050 decision 1)."""

    __tablename__ = "repo_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{}'"),
    )
    build_command: Mapped[str | None] = mapped_column(String, nullable=True)
    test_command: Mapped[str | None] = mapped_column(String, nullable=True)
    lint_command: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_paths: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{}'"),
    )
    components: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{}'"),
    )
    ci: Mapped[str | None] = mapped_column(String, nullable=True)
    has_agent_context: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint("repo", name="uq_repo_profiles_repo"),
    )


class RepoContextDocRow(Base):
    """Index of S3-backed onboarding context docs per (repo, doc_path)."""

    __tablename__ = "repo_context_docs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    doc_path: Mapped[str] = mapped_column(String, nullable=False)
    s3_key: Mapped[str] = mapped_column(String, nullable=False)
    content_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "repo",
            "doc_path",
            "version",
            name="uq_repo_context_docs_repo_doc_version",
        ),
        Index("ix_repo_context_docs_repo_doc", "repo", "doc_path"),
    )
