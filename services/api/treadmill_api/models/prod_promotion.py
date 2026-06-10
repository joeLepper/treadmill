"""Prod-promotion proposals per ADR-0088.

One row per proposal. The row is the current-status read (what the
promote workflow re-verifies against); the events table carries the
audit trail (``prod_promotion.*``, emitted alongside every transition).

Status transitions are compare-and-swap: ``UPDATE ... WHERE status =
'<expected>' AND expires_at > now()`` — single-use (contract invariant 2)
and the expiry hole (Carla's #303 review) both fall out of the guard, not
application logic. Lifecycle::

    proposed ──approve──▶ approved ──workflow──▶ started ──▶ succeeded
        │                                                  └▶ failed
        ├──reject──▶ rejected
        └──(expires_at passes, lazy on read)──▶ expired
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base

# Every state a proposal can be in. Kept as a plain tuple (not a PG enum)
# so adding states is a code change, not a migration.
PROD_PROMOTION_STATUSES = (
    "proposed",
    "approved",
    "rejected",
    "expired",
    "started",
    "succeeded",
    "failed",
)


class ProdPromotion(Base):
    __tablename__ = "prod_promotions"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'proposed'")
    )
    # The full propose bundle from the contract doc (digests,
    # staging_evidence, diff_summary, diff_anchor, env_from/env_to,
    # proposed_by). Opaque here; the payload classes validate shape.
    bundle: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
