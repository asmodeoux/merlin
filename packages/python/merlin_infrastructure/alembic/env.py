"""Alembic env — async engine wired against merlin_infrastructure.db.models.

Sprint 1 fills `target_metadata` with the real `Base.metadata`. For now
the env supports `alembic revision --autogenerate` running against an
empty schema so the first migration is the initial schema.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context
from merlin_infrastructure.db.models import Base
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

if TYPE_CHECKING:
    pass

# Alembic Config object — provides access to .ini values.
config = context.config

# Set up logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the alembic.ini DSN with $DATABASE_URL when present (so CI
# and dev compose can point at their own DBs without editing the .ini).
# Only for CLI runs that load alembic.ini: callers that build their own
# Config() and set sqlalchemy.url (the integration tests) keep their URL.
database_url = os.environ.get("DATABASE_URL")
if database_url and config.config_file_name is not None:
    config.set_main_option("sqlalchemy.url", database_url)


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL instead of executing."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Synchronous core of run_migrations_online — invoked from the async helper."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode via the async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry-point for `alembic upgrade`/`downgrade` when DB is reachable."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
