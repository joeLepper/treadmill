"""Accessor for the host registry tables — ADR-0095 named agents bind to hosts.

Same pattern as :mod:`treadmill_api.team_config_store`: every method takes an
``AsyncSession`` and the caller owns the surrounding transaction.

The label→host binding is write-once-per-label by design: ``bind_label``
upserts, but there is NO code path that rebinds automatically on
host-unreachable (ADR-0095 Task 3 invariant).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models.host import Host, SessionHostBinding


class HostRegistryStore:
    """Async accessor for the ``hosts`` and ``session_host_bindings`` tables."""

    async def register_host(
        self, session: AsyncSession, name: str, tailnet_addr: str | None = None
    ) -> Host:
        """Upsert a host by name. Returns the persisted row.

        If ``tailnet_addr`` is provided, it is written on every call so the
        host can refresh its Tailscale address on re-registration.
        """
        stmt = (
            pg_insert(Host)
            .values(name=name, tailnet_addr=tailnet_addr)
            .on_conflict_do_update(
                index_elements=["name"],
                set_={"tailnet_addr": tailnet_addr},
            )
        )
        await session.execute(stmt)
        row = await session.scalar(sa.select(Host).where(Host.name == name))
        assert row is not None, "upsert must yield a row"
        return row

    async def list_hosts(self, session: AsyncSession) -> list[Host]:
        result = await session.scalars(sa.select(Host).order_by(Host.name))
        return list(result)

    async def bind_label(
        self,
        session: AsyncSession,
        label: str,
        host: str,
        bound_by: str | None = None,
    ) -> SessionHostBinding:
        """Upsert the label→host binding. This is the ONLY write path for bindings.

        Explicit writes only — no caller may rebind automatically on
        host-unreachable (ADR-0095 invariant).
        """
        stmt = (
            pg_insert(SessionHostBinding)
            .values(label=label, host=host, bound_by=bound_by)
            .on_conflict_do_update(
                index_elements=["label"],
                set_={
                    "host": host,
                    "bound_by": bound_by,
                    "bound_at": sa.text("now()"),
                },
            )
        )
        await session.execute(stmt)
        row = await session.scalar(
            sa.select(SessionHostBinding).where(SessionHostBinding.label == label)
        )
        assert row is not None, "upsert must yield a row"
        return row

    async def host_for_label(
        self, session: AsyncSession, label: str
    ) -> str | None:
        """Return the bound host name for a label, or None if unbound."""
        row = await session.scalar(
            sa.select(SessionHostBinding).where(SessionHostBinding.label == label)
        )
        return row.host if row is not None else None

    async def labels_on_host(
        self, session: AsyncSession, host: str
    ) -> list[str]:
        """Return every label bound to the given host."""
        result = await session.scalars(
            sa.select(SessionHostBinding.label)
            .where(SessionHostBinding.host == host)
            .order_by(SessionHostBinding.label)
        )
        return list(result)

    async def unbind_label(self, session: AsyncSession, label: str) -> bool:
        """Remove the binding for a label. Returns True if a row was deleted."""
        result = await session.execute(
            sa.delete(SessionHostBinding).where(SessionHostBinding.label == label)
        )
        return (result.rowcount or 0) > 0
