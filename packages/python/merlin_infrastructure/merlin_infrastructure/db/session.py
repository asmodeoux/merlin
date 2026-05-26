"""Async SQLAlchemy engine + session factory.

Sprint 1 fills in `create_engine_from_settings()` + the `AsyncSession`
factory. For now this module declares the public surface so other code
can `from merlin_infrastructure.db.session import get_session` without
the import landing on a NameError.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an `AsyncSession` for use as a FastAPI dependency or worker callback.

    Sprint 1 wires this up against a real `async_sessionmaker` configured
    from `merlin_api.config.Settings.database_url` (or the equivalent
    worker setting).
    """
    raise NotImplementedError("Sprint 1 — wire async_sessionmaker here")
    yield  # pragma: no cover  # tells the type checker this is a generator
