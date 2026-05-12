"""Redis client factory.

Per ADR-0011, Redis serves dependency-resolution sets and pending-event
buffering (the cache-then-heal pattern). The factory returns ``None`` when
``REDIS_URL`` is unset so the API can boot without Redis; tests exercise
both paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.asyncio as redis_async

if TYPE_CHECKING:
    from treadmill_api.config import Settings


def make_redis(settings: Settings) -> redis_async.Redis | None:
    """Return an async Redis client if REDIS_URL is configured; else None.

    The client is async (``redis.asyncio``) to match FastAPI's loop.
    """
    if not settings.redis_url:
        return None
    return redis_async.Redis.from_url(
        settings.redis_url,
        decode_responses=False,
    )
