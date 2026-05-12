"""FastAPI dependencies for database access.

Provides ``get_session`` — yields an ``AsyncSession`` per request, bound
to the engine attached to ``app.state`` by the lifespan handler. Routes
declare ``session: AsyncSession = Depends(get_session)`` to receive one.

The session factory is constructed once per request (cheap) so we do not
share factories across requests; the engine itself is shared.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


async def get_engine(request: Request) -> AsyncEngine:
    """Return the app-scoped engine. 503 if DATABASE_URL was unset at
    startup (the API can boot without a database for healthcheck inspection,
    but routes that need DB access fail fast)."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not configured (set DATABASE_URL).",
        )
    return engine


async def get_session(
    engine: AsyncEngine = Depends(get_engine),
) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
