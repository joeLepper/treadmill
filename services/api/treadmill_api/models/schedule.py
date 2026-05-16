"""Schedule — first-class DB row per ADR-0035 §Decision.

A Schedule drives periodic workflow dispatches. Each row carries a cron
expression + workflow binding; the scheduler subprocess reads active rows
and emits ``scheduled.tick.<schedule_id>`` events into the event bus
(ADR-0011). Quiet-hour + jitter configuration follows RAMJAC's scrape
scheduler design (ADR-0035 §References).

``payload_template`` is the third explicit JSONB site in Treadmill (the
first two are ``events.payload`` and ``workflow_run_steps.output``).
Access must go through a Pydantic model; raw dict access is forbidden.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False)
    """Standard 5-field cron expression (minute resolution) per ADR-0035 Q35.b."""

    workflow_id: Mapped[str] = mapped_column(String(64), nullable=False)
    """The workflow slug to dispatch on each tick."""

    payload_template: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    """JSON template for the dispatch payload. Third explicit JSONB site
    per ADR-0011 exception granted by ADR-0035."""

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'active'"),
    )
    """``active`` | ``paused``. Enforced by ck_schedules_status CHECK."""

    jitter_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("60"),
    )
    """Per-schedule jitter cap in seconds (RAMJAC precedent, ADR-0035 Q35.d)."""

    quiet_hours: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """Quiet-hour window in ``"HH-HH"`` format (e.g. ``"20-4"`` = 8pm–4am).
    NULL means no quiet hours. Wraparound-aware per RAMJAC precedent."""

    quiet_tz: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default=text("'America/Los_Angeles'"),
    )
    """IANA timezone for quiet_hours evaluation (ADR-0035 Q35.g)."""

    quiet_multiplier: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        server_default=text("6.0"),
    )
    """Interval multiplier during quiet hours (RAMJAC default: 6×)."""

    quiet_max_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("43200"),
    )
    """Hard cap on quiet-hour backoff in seconds (RAMJAC default: 12 h)."""

    last_fired_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    """Timestamp of the most recent successful tick. NULL until first fire."""

    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused')",
            name="ck_schedules_status",
        ),
        Index("ix_schedules_status", "status"),
        Index("ix_schedules_workflow_id", "workflow_id"),
    )
