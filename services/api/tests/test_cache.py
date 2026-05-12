"""Unit tests for treadmill_api.cache — Redis client factory."""

from __future__ import annotations

import redis.asyncio as redis_async

from treadmill_api.cache import make_redis
from treadmill_api.config import Settings


def test_make_redis_returns_none_when_url_unset():
    settings = Settings(redis_url=None)
    assert make_redis(settings) is None


def test_make_redis_returns_async_client_when_url_set():
    settings = Settings(redis_url="redis://localhost:6379/0")
    client = make_redis(settings)
    assert client is not None
    assert isinstance(client, redis_async.Redis)
