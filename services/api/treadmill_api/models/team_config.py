"""``team_configs`` ORM model — coordinator/worker label registry per repo.

Task C of the combined ADR-0085+0086 plan. One row per repo:
the coordinator label that owns dispatch + the list of worker labels
that may receive tasks for that repo.

``GET /api/v1/queue_depth`` joins through this table to exclude
coordinator-authored tasks from the visible / in-flight counts so the
operator-facing depth reads only on tasks coordinators have to route.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import ARRAY, TEXT, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class TeamConfig(Base):
    __tablename__ = "team_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    repo: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    coordinator_label: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_label: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    """Evaluator session label for the per-repo team (ADR-0087). One
    evaluator per repo; reads PRs after CI + peer review, issues
    approve/rework verdicts via cc-relay to the coordinator. Nullable
    so pre-ADR-0087 rows survive the migration; ``treadmill team up``
    populates it for the new model."""
    worker_labels: Mapped[list[str]] = mapped_column(
        ARRAY(TEXT()),
        nullable=False,
        server_default=text("'{}'::text[]"),
    )
    lifecycle: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'ephemeral'"),
    )
    """Team lifecycle mode (ADR-0109): ``ephemeral`` (default — stand up on the first
    plan, tear down when the plan is done), ``persistent`` (never auto-down, hot
    repos), or ``manual`` (up/down only by explicit command). Server-default
    ``ephemeral`` so pre-ADR-0109 rows adopt the resource-conserving default."""
    merge_target: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'feature-branch'"),
    )
    """Where the team integrates (ADR-0110): ``feature-branch`` (default — the team
    merges task PRs into a per-plan ``joes-agents/<slug>`` branch and the human owns
    the branch->main PR) or ``main`` (only the rare repo that permits agent-merge to
    main). Server-default ``feature-branch`` — the safe default for the common case
    where agent-merge-to-main is blocked."""
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
