"""Alembic environment configuration.

Pulls the database URL from ``treadmill_api.config.Settings`` at run time
rather than reading it from ``alembic.ini``. This keeps a single source of
truth for the URL across the application and migration tooling.

Migrations run synchronously (psycopg). The application uses asyncpg, but
alembic is a CLI tool and synchronous execution is simpler. We rewrite a
``postgresql+asyncpg://`` URL to ``postgresql+psycopg://`` here.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from treadmill_api.config import get_settings
from treadmill_api.database import Base

# Importing the models package registers every model on Base.metadata.
# Without this, alembic --autogenerate and the online migration path
# would not see the schema.
from treadmill_api import models  # noqa: F401

# This is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_sync_url() -> str:
    """Convert the application's async DATABASE_URL into a sync form for
    alembic. Raise if no URL is configured AND we're running online —
    alembic always needs one to actually run migrations.

    In offline / ``--sql`` mode alembic only generates DDL (no DB
    connection), so a placeholder URL is acceptable. This lets the
    ADR-0080 alembic-migration-runnable rule-check run
    ``alembic upgrade --sql head`` in the worker sandbox without a
    live database — the DDL output is the gate's input, not a DB
    connection.
    """
    settings = get_settings()
    if not settings.database_url:
        if context.is_offline_mode():
            # Offline / --sql mode: dialect URL is enough to drive the
            # generator. No connection is opened.
            return "postgresql+psycopg://placeholder:placeholder@localhost/placeholder"
        raise RuntimeError(
            "DATABASE_URL is not set. Alembic requires a database URL; "
            "set DATABASE_URL in the environment before invoking alembic."
        )
    url = settings.database_url
    return url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = _resolve_sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_sync_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
