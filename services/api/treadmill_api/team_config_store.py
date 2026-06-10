"""Accessor for the ``team_configs`` table — coordinator/worker label
registry per repo. Task C of the combined ADR-0085+0086 plan.

Same pattern as :mod:`treadmill_api.onboarding_store`: every method
takes an ``AsyncSession`` and the caller owns the surrounding
transaction. Returns ORM rows directly (no separate dataclass shape) —
the ``team_configs`` row IS the dataclass the rest of the API needs.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models import TeamConfig


class TeamConfigStore:
    """Async accessor for the ``team_configs`` table."""

    async def get_by_repo(
        self, session: AsyncSession, repo: str
    ) -> TeamConfig | None:
        return await session.scalar(
            sa.select(TeamConfig).where(TeamConfig.repo == repo)
        )

    async def upsert(
        self,
        session: AsyncSession,
        repo: str,
        coordinator_label: str,
        worker_labels: list[str],
        evaluator_label: str | None = None,
    ) -> TeamConfig:
        """Insert or update by ``repo``. Returns the persisted row.

        ``evaluator_label`` is the ADR-0087 per-repo evaluator session
        label. Optional for back-compat with pre-ADR-0087 callers; new
        ``treadmill team up`` flows populate it.
        """
        stmt = (
            pg_insert(TeamConfig)
            .values(
                repo=repo,
                coordinator_label=coordinator_label,
                evaluator_label=evaluator_label,
                worker_labels=list(worker_labels),
            )
            .on_conflict_do_update(
                index_elements=["repo"],
                set_={
                    "coordinator_label": coordinator_label,
                    "evaluator_label": evaluator_label,
                    "worker_labels": list(worker_labels),
                    "updated_at": sa.text("now()"),
                },
            )
        )
        await session.execute(stmt)
        row = await self.get_by_repo(session, repo)
        assert row is not None, "upsert must yield a row"
        return row

    async def list_all(self, session: AsyncSession) -> list[TeamConfig]:
        result = await session.scalars(
            sa.select(TeamConfig).order_by(TeamConfig.repo)
        )
        return list(result)

    async def delete(self, session: AsyncSession, repo: str) -> bool:
        """Delete the row for ``repo``. Returns True if a row was deleted."""
        result = await session.execute(
            sa.delete(TeamConfig).where(TeamConfig.repo == repo)
        )
        return (result.rowcount or 0) > 0
