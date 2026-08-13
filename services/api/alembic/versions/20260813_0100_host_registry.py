"""Host registry + per-label host binding — ADR-0095.

Creates two tables:
- ``hosts``: registry of named hosts (name is unique). Includes a nullable
  ``tailnet_addr`` (Tailscale IPv4, 100.x) self-reported by the host on
  registration. The ADR-0097 operator plane (Carla) uses this to resolve
  host → WebSocket endpoint. NULL means host not yet reachable.
- ``session_host_bindings``: explicit label→host mapping. The ``label``
  column is the PK (unique per label). Binding is write-only: no automatic
  rebind path exists.

Revision ID: 20260813_0100
Revises: 20260612_0200
Create Date: 2026-08-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260813_0100"
down_revision: Union[str, None] = "20260612_0200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hosts",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("tailnet_addr", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("name", name="uq_hosts_name"),
    )

    op.create_table(
        "session_host_bindings",
        sa.Column("label", sa.String(255), primary_key=True, nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column(
            "bound_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("bound_by", sa.String(255), nullable=True),
    )

    op.create_index(
        "ix_session_host_bindings_host",
        "session_host_bindings",
        ["host"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_host_bindings_host", table_name="session_host_bindings")
    op.drop_table("session_host_bindings")
    op.drop_table("hosts")
