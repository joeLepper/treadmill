"""Central event log ORM model — ADR-0093.

The event log is the Postgres table that backs agent-to-agent messaging.
Every message is appended here by the outbox pump; per-host consumers read
messages addressed to their bound labels and deliver them via cc-relay.

``offset`` is a BIGSERIAL primary key — the append position on the log.
Consumers use ``offset > cursor`` to read new messages efficiently.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, String, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class EventLog(Base):
    __tablename__ = "event_log"

    # Monotonically increasing append position. Consumers track cursor here.
    offset: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # ordering_key = recipient label. Messages to a given label are ordered by
    # ascending offset (ADR-0093 §Order: ordering key is the recipient label).
    ordering_key: Mapped[str] = mapped_column(String(255), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
