"""Plan — first-class entity per ADR-0010.

A Plan is the parent of one or more Tasks. Every Task belongs to exactly
one Plan; small fixes get an implicit one-task Plan. State machine:

    drafting → planning → active → completed | abandoned

Status is derived from events (per ADR-0011); this table holds intent +
metadata only. The future ``plan_status`` VIEW computes status from the
event log; v0 ships without it (Day 2C focuses on ``task_status``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Free-text brief from the submitter; null for plans authored from a
    pre-existing doc on disk (Scenario 1 in ADR-0010)."""

    doc_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Repo-relative path to the plan markdown; null until the plan transitions
    to ``active`` (Scenario 1) or until ``wf-plan`` produces it (Scenario 2)."""

    parent_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Reserved for future hierarchy; unused at v1 per ADR-0010."""

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_plans_repo", "repo"),
        Index("ix_plans_parent_plan_id", "parent_plan_id"),
    )
