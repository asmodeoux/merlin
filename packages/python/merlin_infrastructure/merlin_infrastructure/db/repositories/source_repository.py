"""``SourceRepository`` — SQLAlchemy adapter for ``SourceRepositoryPort``.

Implements the read + write surface used by the onboarding endpoints
(``POST /invites/redeem``, ``POST /sources/admin_bootstrap``,
``PATCH /me``). The application layer holds an ``AsyncSession`` and
COMMITs at the end of the request; repository methods flush so
integrity errors surface inside the use case, but never commit on
their own — the redeem path needs three writes in a single
transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from merlin_core.application.ports import (
    SourceAlreadyExistsError,
    SourceRecord,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from merlin_infrastructure.db.models import Source

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _to_record(row: Source) -> SourceRecord:
    return SourceRecord(
        source_id=row.source_id,
        display_name=row.display_name,
        kind=row.kind,
        reliability=row.reliability,
        credibility=row.credibility,
        consent_version=row.consent_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SourceRepository:
    """SQLAlchemy adapter for ``SourceRepositoryPort``.

    Caller-managed TX: every method flushes; commit happens at the
    application boundary (a FastAPI dependency or worker job).
    """

    def __init__(self, session: AsyncSession) -> None:
        """Construct the repository bound to an open ``AsyncSession``."""
        self._session = session

    async def get_by_source_id(self, source_id: str) -> SourceRecord | None:
        """Return the source if present, else ``None``."""
        row = await self._session.get(Source, source_id)
        if row is None:
            return None
        return _to_record(row)

    async def insert(
        self,
        *,
        source_id: str,
        display_name: str | None,
        kind: str,
        consent_version: str,
    ) -> SourceRecord:
        """INSERT a new ``sources`` row. Raises ``SourceAlreadyExistsError`` on PK clash."""
        row = Source(
            source_id=source_id,
            display_name=display_name,
            kind=kind,
            consent_version=consent_version,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            raise SourceAlreadyExistsError(
                f"source already exists: {source_id}",
            ) from e
        # Refresh so DB-side defaults (created_at, reliability, ...) are populated.
        await self._session.refresh(row)
        return _to_record(row)

    async def update_display_name(self, source_id: str, display_name: str) -> SourceRecord:
        """Update the leaderboard display name. Idempotent on equal value."""
        row = await self._session.get(Source, source_id)
        if row is None:
            raise SourceAlreadyExistsError(f"source not found: {source_id}")
        row.display_name = display_name
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    # Convenience for the redeem path's idempotent-relookup. Not part of the
    # port — the port-level idempotency check goes through
    # ``SourceIdentityRepository.get_source_id_by_tg_user_id`` first.
    async def list_recent(self, limit: int = 20) -> list[SourceRecord]:
        """Return up to ``limit`` most recently created sources (admin debug helper)."""
        result = await self._session.execute(
            select(Source).order_by(Source.created_at.desc()).limit(limit),
        )
        return [_to_record(row) for row in result.scalars().all()]
