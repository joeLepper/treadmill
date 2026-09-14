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
        lifecycle: str | None = None,
        merge_target: str | None = None,
    ) -> TeamConfig:
        """Insert or update by ``repo``. Returns the persisted row.

        ``evaluator_label`` is the ADR-0087 per-repo evaluator session
        label. ``lifecycle`` (ADR-0109) and ``merge_target`` (ADR-0110) are
        optional: when omitted, INSERT uses the column server-defaults
        (``ephemeral`` / ``feature-branch``) and UPDATE PRESERVES the existing
        value — so a plain re-``team up`` never silently resets a repo's mode.
        """
        values: dict[str, object] = {
            "repo": repo,
            "coordinator_label": coordinator_label,
            "evaluator_label": evaluator_label,
            "worker_labels": list(worker_labels),
        }
        set_: dict[str, object] = {
            "coordinator_label": coordinator_label,
            "evaluator_label": evaluator_label,
            "worker_labels": list(worker_labels),
            "updated_at": sa.text("now()"),
        }
        if lifecycle is not None:
            values["lifecycle"] = lifecycle
            set_["lifecycle"] = lifecycle
        if merge_target is not None:
            values["merge_target"] = merge_target
            set_["merge_target"] = merge_target
        stmt = (
            pg_insert(TeamConfig)
            .values(**values)
            .on_conflict_do_update(index_elements=["repo"], set_=set_)
        )
        await session.execute(stmt)
        row = await self.get_by_repo(session, repo)
        assert row is not None, "upsert must yield a row"
        return row

    async def claim(
        self,
        session: AsyncSession,
        repo: str,
        coordinator_label: str,
        worker_labels: list[str],
        evaluator_label: str | None = None,
        lifecycle: str | None = None,
        merge_target: str | None = None,
    ) -> tuple[TeamConfig, bool]:
        """Atomically claim the per-repo STANDUP LEASE (ADR-0109/0110 step 4).

        ``INSERT ... ON CONFLICT (repo) DO NOTHING RETURNING`` — the
        ``team_configs.repo`` UNIQUE constraint is the lock. Under two concurrent
        ``plan.submitted`` for one repo, EXACTLY ONE caller inserts the row and gets
        ``claimed=True`` (it must perform the standup side-effects — render
        templates, start systemd); every other caller gets ``claimed=False`` and the
        already-standing row (it ATTACHES its plan to that team — never a second
        team, never a dropped plan). The losing INSERT blocks on the row lock until
        the winner's transaction commits, then sees the conflict, so the returned
        row is always the winner's committed row.

        Unlike :meth:`upsert`, a conflict does NOT mutate the existing row — an
        attach must never disturb the standing team's labels or mode. The caller
        owns the surrounding transaction; commit to release the lease.

        REQUIRES READ COMMITTED isolation (Postgres + SQLAlchemy default). The
        loser's INSERT unblocks only after the winner commits; the SEPARATE
        ``get_by_repo`` SELECT then takes a fresh snapshot and sees the winner's
        row. Under REPEATABLE READ / SERIALIZABLE the loser's snapshot predates the
        winner's commit, ``get_by_repo`` returns ``None``, and the assertion below
        fires — which in the watcher's plan.submitted handler would DROP the losing
        plan, the exact loss the lease prevents. Keep 4b's claim in a READ COMMITTED
        transaction; do NOT raise the isolation level around this call.
        """
        values: dict[str, object] = {
            "repo": repo,
            "coordinator_label": coordinator_label,
            "evaluator_label": evaluator_label,
            "worker_labels": list(worker_labels),
        }
        if lifecycle is not None:
            values["lifecycle"] = lifecycle
        if merge_target is not None:
            values["merge_target"] = merge_target
        stmt = (
            pg_insert(TeamConfig)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["repo"])
            .returning(TeamConfig.id)
        )
        result = await session.execute(stmt)
        claimed = result.scalar_one_or_none() is not None
        row = await self.get_by_repo(session, repo)
        assert row is not None, "claim must yield a row (inserted or pre-existing)"
        return row, claimed

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
