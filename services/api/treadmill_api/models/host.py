"""Host registry ORM models — ADR-0095 named agents bind to hosts.

Two tables:
- ``hosts``: the registry of known hosts (e.g. "rainbow", "laptop", "core").
- ``session_host_bindings``: maps a session label to the host where it runs.

Identity is (label, host). A label addresses; the host says where the session
runs. The binding is explicit-only: no automatic rebind on host-unreachable.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from treadmill_api.database import Base


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Tailscale IPv4 (100.x), self-reported on registration.
    # NULL = host not yet registered/reachable. Used by ADR-0097 (Carla)
    # to resolve host → WebSocket endpoint.
    tailnet_addr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class SessionHostBinding(Base):
    __tablename__ = "session_host_bindings"

    label: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    # Operator or coordinator that issued the bind — explicit-decision audit trail
    # per ADR-0095 point 5. Nullable because automated registration may not have
    # a named actor.
    bound_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
