"""Unit tests for treadmill_api.database — engine factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from treadmill_api.config import Settings
from treadmill_api.database import Base, make_engine, make_session_factory


def test_make_engine_returns_none_when_url_unset():
    settings = Settings(database_url=None)
    assert make_engine(settings) is None


def test_make_engine_returns_async_engine_when_url_set():
    settings = Settings(database_url="postgresql+asyncpg://u:p@h/db")
    engine = make_engine(settings)
    assert engine is not None
    assert isinstance(engine, AsyncEngine)


def test_session_factory_binds_to_engine():
    settings = Settings(database_url="postgresql+asyncpg://u:p@h/db")
    engine = make_engine(settings)
    assert engine is not None
    session_factory = make_session_factory(engine)
    # Session factory's bind is the engine we passed.
    assert session_factory.kw["bind"] is engine


def test_base_is_a_declarative_base():
    """Base must be importable and usable as the parent of model classes."""
    assert hasattr(Base, "metadata")
    # Models inherit from Base; metadata accumulates as they're imported.
