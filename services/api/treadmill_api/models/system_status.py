"""SystemStatus — autoscaler heartbeat row per family.

Single row per family (K=family, no UUID needed). Carries worker count,
spawn history, and failure tracking for detectors to read.
Updated by autoscaler heartbeat at the end of each tick (success + failure paths).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class SystemStatus(Base):
    __tablename__ = "system_status"

    family: Mapped[str] = mapped_column(String(64), primary_key=True)
    """Worker family identifier."""

    worker_count: Mapped[int] = mapped_column(default=0, nullable=False)
    """Current number of running workers."""

    last_spawn_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    """Timestamp of the most recent successful worker spawn."""

    last_spawn_error: Mapped[str | None] = mapped_column(nullable=True)
    """Truncated error message from the most recent spawn failure."""

    last_consume_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    """Reserved for task 4 (detector consumes from queue)."""

    consecutive_spawn_failures: Mapped[int] = mapped_column(default=0, nullable=False)
    """Count of consecutive spawn failures (reset to 0 on success)."""

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    """Timestamp of the last heartbeat write."""

    __table_args__ = (Index("ix_system_status_updated_at", "updated_at"),)
