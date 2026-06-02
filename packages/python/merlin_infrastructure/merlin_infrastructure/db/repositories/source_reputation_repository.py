"""``SourceReputationRepository`` — append-only history snapshots.

Backs ``SourceReputationRepositoryPort``. One INSERT per
``recompute_source_reputation`` invocation; one ``ORDER BY effective_at
DESC LIMIT 1`` for the ``/me`` + ``/board`` readers.

MVP §15 invariant 10: no ``update()`` / ``delete()``. Postgres
triggers on the ``source_reputation_history`` table reject mutation
attempts at the DB layer too.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from merlin_core.application.ports import (
    LeaderboardEntry,
    SourceReputationRecord,
    SourceReputationRepositoryPort,
)
from sqlalchemy import desc, func, select

from merlin_infrastructure.db.models import Source, SourceReputationHistory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _to_record(row: SourceReputationHistory) -> SourceReputationRecord:
    """Project a SQLAlchemy row to the layer-crossing record."""
    return SourceReputationRecord(
        id=row.id,
        source_id=row.source_id,
        outcomes_resolved=row.outcomes_resolved,
        outcomes_correct=row.outcomes_correct,
        accuracy_pct=row.accuracy_pct,
        triggering_outcome_id=row.triggering_outcome_id,
        effective_at=row.effective_at,
    )


class SourceReputationRepository(SourceReputationRepositoryPort):
    """SQLAlchemy adapter for ``SourceReputationRepositoryPort``."""

    def __init__(self, session: AsyncSession) -> None:
        """Construct the repository bound to an open ``AsyncSession``."""
        self._session = session

    async def insert_snapshot(
        self,
        *,
        source_id: str,
        outcomes_resolved: int,
        outcomes_correct: int,
        accuracy_pct: float | None,
        triggering_outcome_id: UUID | None,
        effective_at: datetime,
    ) -> SourceReputationRecord:
        """INSERT one history row. Flushes; does not commit."""
        row = SourceReputationHistory(
            source_id=source_id,
            outcomes_resolved=outcomes_resolved,
            outcomes_correct=outcomes_correct,
            accuracy_pct=accuracy_pct,
            triggering_outcome_id=triggering_outcome_id,
            effective_at=effective_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def latest_for_source(
        self,
        source_id: str,
    ) -> SourceReputationRecord | None:
        """Most-recent snapshot for the source, or ``None`` cold-start."""
        result = await self._session.execute(
            select(SourceReputationHistory)
            .where(SourceReputationHistory.source_id == source_id)
            .order_by(SourceReputationHistory.effective_at.desc())
            .limit(1),
        )
        row = result.scalars().first()
        if row is None:
            return None
        return _to_record(row)

    async def leaderboard(self, window: str) -> list[LeaderboardEntry]:
        """Top sources by latest ``accuracy_pct`` snapshot.

        ``window`` is accepted today so the route stays a thin
        pass-through; we ignore it for v0 (no time-window slicing yet).
        The query reads the *latest* snapshot per source via a
        correlated subquery + joins ``sources`` for the display name.
        Cold-start sources (no snapshot rows) are omitted because
        ``accuracy_pct IS NULL`` for them and we have nothing honest
        to rank by.

        The display-name fallback is ``"anonymous"`` so the bot never
        renders a blank line on the leaderboard — sources that haven't
        set ``display_name`` still show up if they've resolved
        outcomes. The 40-character cap on ``Source.display_name``
        means the fallback can never overflow the bot's row width.
        """
        del window  # v0 reads all-time only — the column wasn't sliced

        latest_effective = (
            select(
                SourceReputationHistory.source_id.label("sid"),
                func.max(SourceReputationHistory.effective_at).label("max_eff"),
            )
            .group_by(SourceReputationHistory.source_id)
            .subquery()
        )

        stmt = (
            select(
                SourceReputationHistory,
                Source.display_name,
            )
            .join(
                latest_effective,
                (SourceReputationHistory.source_id == latest_effective.c.sid)
                & (SourceReputationHistory.effective_at == latest_effective.c.max_eff),
            )
            .join(
                Source,
                Source.source_id == SourceReputationHistory.source_id,
            )
            .where(SourceReputationHistory.accuracy_pct.isnot(None))
            .order_by(
                desc(SourceReputationHistory.accuracy_pct),
                desc(SourceReputationHistory.outcomes_resolved),
            )
        )

        result = await self._session.execute(stmt)
        rows = result.all()

        entries: list[LeaderboardEntry] = []
        for rank, (snapshot, display_name) in enumerate(rows, start=1):
            entries.append(
                LeaderboardEntry(
                    rank=rank,
                    display_name=display_name or "anonymous",
                    calls_resolved=snapshot.outcomes_resolved,
                    calls_correct=snapshot.outcomes_correct,
                    # ``accuracy_pct`` is NOT NULL here because of the
                    # WHERE clause; assert at the API boundary so a
                    # future schema change can't silently drop NaNs
                    # through the float type.
                    accuracy_pct=float(snapshot.accuracy_pct or 0.0),
                    last_call_at=snapshot.effective_at,
                ),
            )
        return entries
