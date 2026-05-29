"""``SourceIdentityRepository`` — the PII table adapter.

Reads / writes the *only* table where raw Telegram identifiers live
(MVP §13 + §15 invariant 9). Every other repository keys on the opaque
``source_id`` and never sees the raw TG id. Phase-2 adds a GDPR export
path that read-joins through this table; until then the bot's
onboarding is the sole writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from merlin_infrastructure.db.models import SourceIdentity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class SourceIdentityRepository:
    """SQLAlchemy adapter for ``SourceIdentityRepositoryPort``.

    Caller-managed TX (same contract as the other repositories) — the
    redeem flow co-commits the identity row with the source row.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Construct the repository bound to an open ``AsyncSession``."""
        self._session = session

    async def get_source_id_by_tg_user_id(self, tg_user_id: int) -> str | None:
        """Reverse-lookup. Used to detect re-redeems / re-bootstraps.

        Returns the opaque ``source_id`` or ``None`` if the user
        hasn't been onboarded yet.
        """
        result = await self._session.execute(
            select(SourceIdentity.source_id).where(
                SourceIdentity.tg_user_id == tg_user_id,
            ),
        )
        return result.scalar_one_or_none()

    async def insert(
        self,
        *,
        source_id: str,
        tg_user_id: int,
        tg_handle: str | None,
    ) -> None:
        """INSERT a new identity row.

        The caller is responsible for making sure the matching
        ``sources`` row exists in the same transaction — the FK
        constraint will surface a violation otherwise. The redeem use
        case calls ``SourceRepository.insert`` immediately before this
        method, so the order is well-defined.
        """
        row = SourceIdentity(
            source_id=source_id,
            tg_user_id=tg_user_id,
            tg_handle=tg_handle,
        )
        self._session.add(row)
        await self._session.flush()
