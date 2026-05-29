"""Async SQLAlchemy engine + session factory.

A thin module the API and worker both depend on. Builds one async
engine per process (lazy, lru-cached on the DSN) and exposes a session
factory used both directly by the worker and via FastAPI's dependency
system in the API.

Design:

* ``create_engine(dsn)`` — pure constructor; returns a fresh
  ``AsyncEngine`` configured for asyncpg. Sized for a single API replica
  (pool 10, overflow 10). Worker may share the same defaults.
* ``get_session_factory(dsn)`` — cached on the DSN string. Returns an
  ``async_sessionmaker``. Tests pass a different DSN (testcontainers) and
  get a fresh factory; production calls with the same DSN every time and
  reuses the cached factory.
* ``yield_session(factory)`` — pytest-async / FastAPI-friendly generator
  helper. Wraps the session in a try/except/finally that ROLLBACKs on
  exception and CLOSEs always — matches the FastAPI dependency contract
  (one session per request, COMMIT happens inside the repository).

The module is stdlib-friendly: importing it doesn't crash if asyncpg is
absent at import time (FastAPI's collection step), but the engine
constructor will fail loudly the first time a DSN actually triggers a
connect.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(dsn: str) -> AsyncEngine:
    """Build a fresh asyncpg engine. One-shot — call sites cache the result."""
    return create_async_engine(
        dsn,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=4)
def get_session_factory(dsn: str) -> async_sessionmaker[AsyncSession]:
    """Return an ``async_sessionmaker`` cached on the DSN.

    The maxsize=4 cap is intentional — covers (dev DSN, prod DSN,
    testcontainers DSN, one spare). If you find yourself opening more
    distinct DSNs in one process you have a different bug.
    """
    engine = create_engine(dsn)
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


async def yield_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """FastAPI-dependency-shaped session yielder.

    Use as::

        async def get_session(
            settings: Settings = Depends(get_settings),
        ) -> AsyncIterator[AsyncSession]:
            factory = get_session_factory(settings.database_url)
            async for session in yield_session(factory):
                yield session

    On exception the session is rolled back; the close is always
    attempted. The repository is responsible for COMMITting — this
    helper deliberately does NOT commit on success, so unit-of-work
    boundaries stay in the application layer.
    """
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
