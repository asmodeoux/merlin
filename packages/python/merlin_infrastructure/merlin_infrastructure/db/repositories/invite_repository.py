"""``InviteRepository`` — SQLAlchemy adapter for ``InviteRepositoryPort``.

Owns ``invites`` table I/O for the admin issuance path
(``POST /invites``) and the user redemption path
(``POST /invites/redeem``). Redeem is atomic at the SQL layer via a
conditional UPDATE — two racing redeems for the same code can never
both succeed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from merlin_core.application.ports import (
    InviteRecord,
    InviteRepositoryPort,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from merlin_infrastructure.db.models import Invite

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _to_record(row: Invite) -> InviteRecord:
    return InviteRecord(
        id=row.id,
        code=row.code,
        created_by_source_id=row.created_by_source_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        redeemed_at=row.redeemed_at,
        redeemed_by_source_id=row.redeemed_by_source_id,
        revoked_at=row.revoked_at,
    )


class InviteRepository(InviteRepositoryPort):
    """SQLAlchemy adapter for ``InviteRepositoryPort``."""

    def __init__(self, session: AsyncSession) -> None:
        """Construct the repository bound to an open ``AsyncSession``."""
        self._session = session

    async def get_redeemable_by_code(self, code: str) -> InviteRecord | None:
        """Look up a code that's still consumable.

        Filters: ``redeemed_at IS NULL AND revoked_at IS NULL AND
        expires_at > now()``. Used by the redeem endpoint to surface a
        404 *before* attempting the atomic mark — fewer hot-path UPDATEs
        for the common "bad code" case.
        """
        now = datetime.now(tz=UTC)
        result = await self._session.execute(
            select(Invite).where(
                Invite.code == code,
                Invite.redeemed_at.is_(None),
                Invite.revoked_at.is_(None),
                Invite.expires_at > now,
            ),
        )
        row = result.scalar_one_or_none()
        return _to_record(row) if row is not None else None

    async def insert(
        self,
        *,
        code: str,
        created_by_source_id: str | None,
        expires_at: datetime,
    ) -> InviteRecord:
        """INSERT a new invite. Raises on UNIQUE(code) collision."""
        row = Invite(
            code=code,
            created_by_source_id=created_by_source_id,
            expires_at=expires_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            raise ValueError(f"invite code already exists: {code}") from e
        await self._session.refresh(row)
        return _to_record(row)

    async def mark_redeemed(
        self,
        *,
        invite_id: UUID,
        redeemed_by_source_id: str,
        redeemed_at: datetime,
    ) -> bool:
        """Conditional UPDATE — returns True iff exactly one row was claimed.

        Implemented as ``UPDATE ... SET redeemed_at=:t, redeemed_by=:s
        WHERE id=:id AND redeemed_at IS NULL AND revoked_at IS NULL``.
        Two racing redeems will see one True and one False; the loser's
        ``rowcount == 0`` and the application layer raises
        ``InviteAlreadyRedeemedError`` (also covers the rare
        "revoked between SELECT and UPDATE" race).
        """
        stmt = (
            update(Invite)
            .where(
                Invite.id == invite_id,
                Invite.redeemed_at.is_(None),
                Invite.revoked_at.is_(None),
            )
            .values(
                redeemed_at=redeemed_at,
                redeemed_by_source_id=redeemed_by_source_id,
            )
        )
        result = await self._session.execute(stmt)
        # ``Result.rowcount`` exists on ``CursorResult`` (returned by
        # UPDATE/DELETE/INSERT) but not on the parent ``Result``; the
        # mypy plugin can't narrow that. The runtime type is fine.
        return result.rowcount == 1  # type: ignore[attr-defined]
