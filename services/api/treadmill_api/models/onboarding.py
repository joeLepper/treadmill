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

This module also hosts the ADR-0059 ``WorkerDeps`` + ``BinarySpec``
Pydantic models and the side table ``repo_worker_binaries`` row model
that backs them — workers materialize per-repo extras (Python / Node
package lists + downloaded binaries) before invoking task work.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    ARRAY,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


# ── ADR-0059 Pydantic shapes ────────────────────────────────────────────────

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
BINARY_TARGET_PREFIX = "/var/treadmill/repo-bin/"


class BinarySpec(BaseModel):
    """Signed-URL binary download + checksum + materialization path (ADR-0059).

    Workers fetch ``download_url``, verify ``sha256_checksum``, and place
    the result at ``target_path`` (which must live under
    ``/var/treadmill/repo-bin/`` per the ADR's materialization spec).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    download_url: str = Field(..., min_length=1)
    sha256_checksum: str
    target_path: str = Field(..., min_length=1)

    @field_validator("sha256_checksum")
    @classmethod
    def _validate_sha256(cls, v: str) -> str:
        if not _SHA256_PATTERN.fullmatch(v):
            raise ValueError(
                "sha256_checksum must be exactly 64 lowercase hex characters"
            )
        return v

    @field_validator("target_path")
    @classmethod
    def _validate_target_path(cls, v: str) -> str:
        if not v.startswith(BINARY_TARGET_PREFIX):
            raise ValueError(
                f"target_path must start with {BINARY_TARGET_PREFIX!r}"
            )
        return v


class WorkerDeps(BaseModel):
    """ADR-0059: per-repo extras the worker installs before task work.

    Each list is opt-in; an empty ``WorkerDeps()`` means "no extras." Apt
    / OS-package support is intentionally out-of-scope for v1 (privilege
    boundary; see ADR-0059 Out-of-scope).
    """

    model_config = ConfigDict(extra="forbid")

    python: list[str] = []
    node: list[str] = []
    binaries: list[BinarySpec] = []


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
    # Fallback account when the primary hits a usage limit (ADR-0066).
    # ``NULL`` means no fallback configured for this repo.
    claude_account_fallback: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # ADR-0059: per-repo Python / Node deps the worker installs before
    # task work. Binaries live in the ``repo_worker_binaries`` side table.
    worker_deps_python: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{}'"),
    )
    worker_deps_node: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        server_default=text("'{}'"),
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


class RepoWorkerBinaryRow(Base):
    """ADR-0059: side table for per-repo binary downloads.

    One row per registered binary on a repo's :class:`RepoConfigRow`.
    Upserts replace the entire set for the repo (simpler than diffing —
    the list is small and operator-curated).
    """

    __tablename__ = "repo_worker_binaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    repo_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repo_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    download_url: Mapped[str] = mapped_column(String, nullable=False)
    sha256_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    target_path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        Index("ix_repo_worker_binaries_repo_config_id", "repo_config_id"),
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
