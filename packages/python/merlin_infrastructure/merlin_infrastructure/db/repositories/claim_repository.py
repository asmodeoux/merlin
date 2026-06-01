"""``ClaimRepository`` — SQLAlchemy adapter for ``ClaimRepositoryPort``.

Owns ``claims`` table I/O for the minimum slice that makes ``/me`` and
``/board`` honest:

* ``create`` — the ``POST /claims`` write. The route looks up
  ``source_id`` via ``SourceIdentityRepository`` and passes it down to
  this method along with the resolved asset metadata + LLM-extracted
  fields. The use case itself stays job-only (it still enqueues the RQ
  resolution job) — persistence moves here so the row exists by the
  time the worker fires.

* ``list_open_by_source`` — the ``GET /me`` open-calls query. Orders
  by ``resolves_at ASC`` so the soonest-to-resolve sits at the top.

* ``count_resolved_by_source`` — the ``GET /me`` resolved tally.
  Honest proxy for "resolved" until the outcomes table exists.

* ``leaderboard`` — returns ``[]`` for now. The shape is pinned here
  so when the worker writes outcomes, only this method changes.

Append-only invariant (MVP §15 invariant 7): no ``update()`` /
``delete()`` method. Closed-loop columns (``edits`` JSONB +
``soft_deleted_at``) and their write paths ship with the worker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from merlin_core.application.ports import (
    ClaimRecord,
    ClaimRepositoryPort,
    LeaderboardEntry,
)
from sqlalchemy import func, select

from merlin_infrastructure.db.models import Claim

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _to_record(row: Claim) -> ClaimRecord:
    """Project a SQLAlchemy ``Claim`` row to the layer-crossing record."""
    return ClaimRecord(
        id=row.id,
        source_id=row.source_id,
        asset_coingecko_id=row.asset_coingecko_id,
        asset_symbol=row.asset_symbol,
        asset_name=row.asset_name,
        direction=row.direction,
        horizon=row.horizon,
        conviction=row.conviction,
        thesis_text=row.thesis_text,
        asset_query=row.asset_query,
        submitted_at=row.submitted_at,
        resolves_at=row.resolves_at,
        parsed_via=row.parsed_via,
        price_usd_at_submit=row.price_usd_at_submit,
        extracted_fields=dict(row.extracted_fields or {}),
        parse_metadata=dict(row.parse_metadata or {}),
    )


class ClaimRepository(ClaimRepositoryPort):
    """SQLAlchemy adapter for ``ClaimRepositoryPort``.

    Caller-managed TX: every method flushes; commit happens at the
    application boundary (a FastAPI dependency or worker job). The
    route co-commits the claim row with the enqueue side-effect.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Construct the repository bound to an open ``AsyncSession``."""
        self._session = session

    async def create(
        self,
        *,
        claim_id: UUID,
        source_id: str,
        asset_coingecko_id: str,
        asset_symbol: str,
        asset_name: str | None,
        direction: str,
        horizon: str,
        conviction: str,
        thesis_text: str,
        asset_query: str | None,
        submitted_at: datetime,
        resolves_at: datetime,
        parsed_via: str,
        extracted_fields: dict[str, Any],
        parse_metadata: dict[str, Any],
        price_usd_at_submit: float | None = None,
    ) -> ClaimRecord:
        """INSERT a new claim row. Flushes; does not commit."""
        row = Claim(
            id=claim_id,
            source_id=source_id,
            asset_coingecko_id=asset_coingecko_id,
            asset_symbol=asset_symbol,
            asset_name=asset_name,
            direction=direction,
            horizon=horizon,
            conviction=conviction,
            thesis_text=thesis_text,
            asset_query=asset_query,
            submitted_at=submitted_at,
            resolves_at=resolves_at,
            parsed_via=parsed_via,
            price_usd_at_submit=price_usd_at_submit,
            extracted_fields=dict(extracted_fields),
            parse_metadata=dict(parse_metadata),
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def list_open_by_source(self, source_id: str) -> list[ClaimRecord]:
        """Open calls — ``resolves_at`` in the future, oldest-resolving first."""
        now = datetime.now(tz=UTC)
        result = await self._session.execute(
            select(Claim)
            .where(Claim.source_id == source_id, Claim.resolves_at > now)
            .order_by(Claim.resolves_at.asc()),
        )
        return [_to_record(row) for row in result.scalars().all()]

    async def count_resolved_by_source(self, source_id: str) -> int:
        """Count of past-horizon calls. Honest "resolved" proxy until outcomes exist."""
        now = datetime.now(tz=UTC)
        result = await self._session.execute(
            select(func.count())
            .select_from(Claim)
            .where(Claim.source_id == source_id, Claim.resolves_at <= now),
        )
        scalar = result.scalar()
        return int(scalar) if scalar is not None else 0

    async def leaderboard(self, window: str) -> list[LeaderboardEntry]:
        """Top-N source ranking — empty until the outcomes layer lands.

        The ``window`` parameter is accepted today so the route stays a
        thin pass-through; we ignore it because there's nothing to
        window over yet. When outcomes ship, this method gets the
        real query (resolved-claim accuracy with Bayesian shrinkage,
        cold-start hidden per MVP §11.1).
        """
        del window  # unused until the outcomes layer ships
        return []
