"""Accessor for the ADR-0050 onboarding persistence tables.

Wraps the ORM rows defined in :mod:`treadmill_api.models.onboarding`
behind a small async accessor that callers in the onboarding flow can
use without learning the SQLAlchemy upsert dance.

Following the pattern in :mod:`treadmill_api.coordination`, every method
takes an :class:`AsyncSession` and the caller owns the session and the
surrounding transaction (so a single ``async with session.begin():``
block can batch multiple onboarding writes together).

The accessor converts between the dataclass shapes in
:mod:`treadmill_api.repo_config` / :mod:`treadmill_api.repo_profile` and
the ORM rows — callers stay on the dataclasses; the ORM is an
implementation detail.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models.onboarding import (
    RepoConfigRow,
    RepoContextDocRow,
    RepoProfileRow,
)
from treadmill_api.repo_config import RepoConfig
from treadmill_api.repo_profile import RepoProfile


class OnboardingStore:
    """Async accessor for ``repo_configs`` / ``repo_profiles`` /
    ``repo_context_docs``.

    All methods take an ``AsyncSession``; the caller commits.
    """

    async def upsert_repo_config(
        self, session: AsyncSession, config: RepoConfig
    ) -> None:
        existing = await session.scalar(
            sa.select(RepoConfigRow).where(RepoConfigRow.repo == config.repo)
        )
        if existing is None:
            session.add(
                RepoConfigRow(
                    repo=config.repo,
                    mode=config.mode,
                    auto_merge_blocked=config.auto_merge_blocked,
                    test_command=config.test_command,
                    lint_command=config.lint_command,
                    claude_account=config.claude_account,
                )
            )
            return
        existing.mode = config.mode
        existing.auto_merge_blocked = config.auto_merge_blocked
        existing.test_command = config.test_command
        existing.lint_command = config.lint_command
        existing.claude_account = config.claude_account
        existing.updated_at = sa.func.now()

    async def get_repo_config(
        self, session: AsyncSession, repo: str
    ) -> RepoConfig | None:
        row = await session.scalar(
            sa.select(RepoConfigRow).where(RepoConfigRow.repo == repo)
        )
        if row is None:
            return None
        return RepoConfig(
            repo=row.repo,
            mode=row.mode,
            auto_merge_blocked=row.auto_merge_blocked,
            test_command=row.test_command,
            lint_command=row.lint_command,
            claude_account=row.claude_account,
        )

    async def upsert_repo_profile(
        self, session: AsyncSession, profile: RepoProfile
    ) -> None:
        existing = await session.scalar(
            sa.select(RepoProfileRow).where(RepoProfileRow.repo == profile.repo)
        )
        if existing is None:
            session.add(
                RepoProfileRow(
                    repo=profile.repo,
                    languages=list(profile.languages),
                    build_command=profile.build_command,
                    test_command=profile.test_command,
                    lint_command=profile.lint_command,
                    doc_paths=list(profile.doc_paths),
                    components=list(profile.components),
                    ci=profile.ci,
                    has_agent_context=profile.has_agent_context,
                )
            )
            return
        existing.languages = list(profile.languages)
        existing.build_command = profile.build_command
        existing.test_command = profile.test_command
        existing.lint_command = profile.lint_command
        existing.doc_paths = list(profile.doc_paths)
        existing.components = list(profile.components)
        existing.ci = profile.ci
        existing.has_agent_context = profile.has_agent_context
        existing.updated_at = sa.func.now()

    async def get_repo_profile(
        self, session: AsyncSession, repo: str
    ) -> RepoProfile | None:
        row = await session.scalar(
            sa.select(RepoProfileRow).where(RepoProfileRow.repo == repo)
        )
        if row is None:
            return None
        return RepoProfile(
            repo=row.repo,
            languages=list(row.languages),
            build_command=row.build_command,
            test_command=row.test_command,
            lint_command=row.lint_command,
            doc_paths=list(row.doc_paths),
            components=list(row.components),
            ci=row.ci,
            has_agent_context=row.has_agent_context,
        )

    async def record_context_doc(
        self,
        session: AsyncSession,
        repo: str,
        doc_path: str,
        s3_key: str,
        content_sha: str,
    ) -> int:
        """Insert the next version for (repo, doc_path) and return it.

        Versions start at 1 and increase monotonically. The UNIQUE
        constraint on ``(repo, doc_path, version)`` is what keeps
        concurrent writers honest; callers wrap this in a transaction.
        """
        current_max = await session.scalar(
            sa.select(sa.func.max(RepoContextDocRow.version)).where(
                RepoContextDocRow.repo == repo,
                RepoContextDocRow.doc_path == doc_path,
            )
        )
        next_version = (current_max or 0) + 1
        session.add(
            RepoContextDocRow(
                repo=repo,
                doc_path=doc_path,
                s3_key=s3_key,
                content_sha=content_sha,
                version=next_version,
            )
        )
        return next_version

    async def get_context_doc(
        self, session: AsyncSession, repo: str, doc_path: str
    ) -> RepoContextDocRow | None:
        return await session.scalar(
            sa.select(RepoContextDocRow)
            .where(
                RepoContextDocRow.repo == repo,
                RepoContextDocRow.doc_path == doc_path,
            )
            .order_by(RepoContextDocRow.version.desc())
            .limit(1)
        )

    async def list_repo_docs(
        self, session: AsyncSession, repo: str
    ) -> list[RepoContextDocRow]:
        """Return the latest-version row per ``doc_path`` for ``repo``.

        One row per distinct ``doc_path`` (the row whose ``version``
        equals the per-path ``MAX(version)``), ordered by ``doc_path``.
        Used by the dashboard's ``GET /api/v1/dashboard/repos/{repo}/docs``
        surface to compute the arch / plans / last_updated summary
        without each caller re-deriving the latest-version join.
        """
        latest = (
            sa.select(
                RepoContextDocRow.doc_path,
                sa.func.max(RepoContextDocRow.version).label("max_version"),
            )
            .where(RepoContextDocRow.repo == repo)
            .group_by(RepoContextDocRow.doc_path)
            .subquery()
        )
        result = await session.execute(
            sa.select(RepoContextDocRow)
            .join(
                latest,
                sa.and_(
                    RepoContextDocRow.doc_path == latest.c.doc_path,
                    RepoContextDocRow.version == latest.c.max_version,
                ),
            )
            .where(RepoContextDocRow.repo == repo)
            .order_by(RepoContextDocRow.doc_path)
        )
        return list(result.scalars().all())
