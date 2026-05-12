"""Async SQLAlchemy engine + session factory.

Per ADR-0011, persistence is Postgres; engine is created at app startup and
disposed at shutdown. The factory returns ``None`` when ``DATABASE_URL`` is
unset so the API can boot for healthcheck-only inspection during local
development without a database. Tests exercise both wired and unwired paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

if TYPE_CHECKING:
    from treadmill_api.config import Settings


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy models in the API service."""

    pass


def make_engine(settings: Settings) -> AsyncEngine | None:
    """Return an AsyncEngine if DATABASE_URL is configured; else None.

    The URL must be in async form (``postgresql+asyncpg://...``); we do not
    rewrite synchronous URLs because the alembic config uses sync URLs and
    we keep the boundary explicit.
    """
    if not settings.database_url:
        return None
    return create_async_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to the given engine."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
