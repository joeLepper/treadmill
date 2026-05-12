"""Dispatch dedup table per ADR-0026.

The trigger evaluator inserts one row per dispatched workflow_run, keyed
by a deterministic ``dedup_key`` built from the event payload. A PK
collision on the key is the dedup mechanism — concurrent or redundant
events trying to dispatch the same workflow run land on the constraint
and skip.

Per ADR-0026 §"Optimistic pre-check + PK gate ordering" the
``workflow_run_id`` column is intentionally NOT a foreign key. The
insert-first/dispatch-second flow writes a sentinel-valued row before
the run exists, then updates ``workflow_run_id`` once the run is
created. Adding a FK would force either a deferrable constraint or a
nullable column; v0 picks "no FK" so the constraint pattern is just the
PK.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class WorkflowDispatchDedup(Base):
    """One row per dispatched workflow_run that has a deterministic
    dedup key. The PK on ``dedup_key`` is the dedup mechanism."""

    __tablename__ = "workflow_dispatch_dedup"

    dedup_key: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
    )
    """Deterministic key built from the event payload, shape
    ``<workflow_id>:<repo>:<discriminator>`` per ADR-0026's
    "Discriminator parts" table."""

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    """The workflow_run this dedup row gated. Initially written with a
    sentinel value (zeros UUID) by the pre-dispatch helper, then UPDATEd
    to the real run's id once dispatch_task completes. No FK per
    ADR-0026 — see the module docstring."""

    dispatched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
