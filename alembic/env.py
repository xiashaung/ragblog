"""Alembic async environment.

Reads ``APP_DATABASE_URL`` (SQLite by default) and autogenerates migrations
from ``app.models``. Uses an async engine so it works with the async
SQLAlchemy setup used in the app.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base  # noqa: F401  (populates Base.metadata)

# Alembic Config object, provides access to values within the `alembic.ini`
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_settings().database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Connect to the database using an async engine and run migrations."""
    url = get_settings().database_url
    connect_args: dict = (
        {"check_same_thread": False} if url.startswith("sqlite") else {}
    )
    engine = create_async_engine(
        url, poolclass=pool.NullPool, connect_args=connect_args
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (async)."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
