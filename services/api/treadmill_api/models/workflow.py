"""Workflow + role configuration per ADR-0010.

Workflows are versioned (snapshot-on-submission). A WorkflowVersion's
steps reference a Role; each Role composes a model + system prompt with
ordered Skills and Hooks.

Eight tables live in this module:
  - workflows               — slug-keyed workflow definitions
  - workflow_versions       — immutable version rows; tasks pin to one
  - workflow_version_steps  — ordered steps within a version
  - roles                   — agent configuration (model + prompt + tier)
  - skills                  — reusable skill content
  - hooks                   — reusable hook commands
  - role_skills             — many-to-many (ordered) role ↔ skill
  - role_hooks              — many-to-many (ordered) role ↔ hook
  - event_triggers          — (event_type, repo) → workflow_id

Roles drop bunkhouse's ``base_profile_id`` (third layer of composition);
ADR-0010 commits to three layers: model + system_prompt + (skills, hooks).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class OutputKind(StrEnum):
    """Per ADR-0022 — what kind of output a Role produces.

    The runner dispatches its post-Claude-Code disposition on this field:

      * ``CODE`` — diff → commit → push → PR. Empty diff is failure.
      * ``REVIEW`` — empty diff is success; runner posts ``gh pr review``.
      * ``ANALYSIS`` — empty diff is success; output flows to downstream step
        via the existing ADR-0015 step-output composition.
      * ``PLAN_DOC`` — like ``CODE`` but the diff MUST be confined to
        ``docs/plans/``.

    Spellings are lowercase snake_case per ADR-0016's canonical-spellings
    discipline. The Ralph-loop validation pattern reserves the word
    "validation"; this enum deliberately does not include it (see ADR-0022
    §"Scope discipline: 'validation' is reserved").
    """

    CODE = "code"
    REVIEW = "review"
    ANALYSIS = "analysis"
    PLAN_DOC = "plan_doc"


class Workflow(Base):
    """Slug-identified workflow (e.g. ``wf-author``). Mutable metadata only;
    the semantically-meaningful definition lives on WorkflowVersion."""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class WorkflowVersion(Base):
    """An immutable snapshot of a workflow's step list.

    Tasks pin to a specific WorkflowVersion via Task.workflow_version_id.
    Editing a Workflow's step list creates a new WorkflowVersion row; the
    pinned tasks are not affected.
    """

    __tablename__ = "workflow_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    workflow_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_version"),
        Index("ix_workflow_versions_workflow_id", "workflow_id"),
    )


class WorkflowVersionStep(Base):
    """One step within a workflow version, in order."""

    __tablename__ = "workflow_version_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("roles.id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "workflow_version_id", "step_index",
            name="uq_workflow_version_steps_index",
        ),
        Index("ix_workflow_version_steps_workflow_version_id", "workflow_version_id"),
    )


class Role(Base):
    """Agent configuration: model + system prompt + compute tier.

    Composition with skills and hooks is via ``role_skills`` and ``role_hooks``
    join tables (preserving order).
    """

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    output_kind: Mapped[OutputKind] = mapped_column(
        SAEnum(
            OutputKind,
            name="output_kind",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
            native_enum=False,
            length=32,
        ),
        nullable=False,
    )
    """Per ADR-0022 — declares what the worker does with this role's
    Claude Code output. The runner's per-kind dispatch table reads this
    field to pick the right disposition handler."""
    # Reserved for the future multi-tier ADR; v0 ships single-tier only (no wire field).
    compute_tier: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'standard'"),
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


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
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


class Hook(Base):
    __tablename__ = "hooks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    matcher: Mapped[str | None] = mapped_column(String(255), nullable=True)
    command: Mapped[str] = mapped_column(Text, nullable=False)
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


class RoleSkill(Base):
    """Ordered role↔skill membership."""

    __tablename__ = "role_skills"

    role_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )
    skill_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("role_id", "skill_id", name="pk_role_skills"),
        Index("ix_role_skills_role_id", "role_id"),
    )


class RoleHook(Base):
    """Ordered role↔hook membership."""

    __tablename__ = "role_hooks"

    role_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )
    hook_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("hooks.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("role_id", "hook_id", name="pk_role_hooks"),
        Index("ix_role_hooks_role_id", "role_id"),
    )


class EventTrigger(Base):
    """(repo, event_type) → workflow rule.

    Maps an incoming event (e.g. ``pr_opened`` from a GitHub webhook on
    repo ``RAMJAC/myapp``) to the workflow that should run against
    the matched task. ``version_strategy`` decides which WorkflowVersion to
    pin: ``"latest"`` looks up the highest version at trigger time;
    ``"pinned:<version>"`` uses a specific version.
    """

    __tablename__ = "event_triggers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    repo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """``null`` matches all repos. Specific repo names take precedence."""

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_strategy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'latest'"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "repo", "event_type",
            name="uq_event_triggers_repo_event",
        ),
        Index("ix_event_triggers_event_type", "event_type"),
    )
