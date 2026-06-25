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

The leaderboard read lives on ``SourceReputationRepository`` (it queries
``source_reputation_history``, not raw claims) — that keeps the
"reputation = hit rate of resolved outcomes" coupling on one repo and
out of this one.

Append-only invariant (MVP §15 invariant 7): no ``update()`` /
``delete()`` method. Closed-loop columns (``edits`` JSONB +
``soft_deleted_at``) and their write paths ship with the worker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from merlin_core.application.ports import (
    ClaimRecord,
    ClaimRepositoryPort,
)
from sqlalchemy import exists, func, select

from merlin_infrastructure.db.models import Claim, Outcome

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
        horizon_seconds=row.horizon_seconds,
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
        horizon_seconds: int,
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
            horizon_seconds=horizon_seconds,
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

    async def list_unresolved_overdue(
        self,
        *,
        grace_seconds: int,
        limit: int,
    ) -> list[UUID]:
        """Claim ids past ``resolves_at + grace`` that still have no outcome.

        The reconciliation backstop (Q-030). The ``POST /claims`` path
        schedules resolution as an RQ ``enqueue_at`` job in Redis; if
        that job is ever lost (Upstash eviction of the long-lived
        ``rq:scheduled`` / ``rq:job`` keys, a worker OOM-SIGKILL that
        orphans the in-flight job, a Redis restart without persistence),
        the durable ``claims`` row is the only surviving record that a
        resolution is owed. This query is how the sweep finds those
        stranded bets so it can re-enqueue ``resolve_outcome`` for each.

        SQL contract:

        .. code-block:: sql

            SELECT c.id
            FROM claims c
            WHERE c.resolves_at < now() - (:grace_seconds * interval '1 second')
              AND NOT EXISTS (
                SELECT 1 FROM outcomes o WHERE o.claim_id = c.id
              )
            ORDER BY c.resolves_at ASC
            LIMIT :limit

        Notes:
        * ``grace_seconds`` keeps the sweep off bets that have only
          *just* come due — RQ's own ``enqueue_at`` + ``Retry(max=3)``
          window gets first crack. MVP §12.1 sets this at 6 h.
        * ``NOT EXISTS`` (not a ``LEFT JOIN ... IS NULL``) so a claim
          with *any* outcome row — ``resolved`` / ``rejected_asset_mutation``
          / ``resolution_failed_final`` — is considered handled and is
          never re-enqueued. The resolver is also idempotent on top of
          this (it short-circuits when ``latest_for_claim`` is non-NULL),
          so a row landing between this read and the re-enqueue is
          harmless.
        * ``ORDER BY resolves_at ASC`` drains the most-overdue bets
          first; ``LIMIT`` bounds one sweep so a large backlog doesn't
          enqueue tens of thousands of jobs in a single tick.
        """
        cutoff = datetime.now(tz=UTC) - timedelta(seconds=grace_seconds)
        no_outcome = ~exists().where(Outcome.claim_id == Claim.id)
        result = await self._session.execute(
            select(Claim.id)
            .where(Claim.resolves_at < cutoff, no_outcome)
            .order_by(Claim.resolves_at.asc())
            .limit(limit),
        )
        return [row[0] for row in result.all()]

    async def count_open_global(self) -> tuple[int, int]:
        """Platform-wide ``(open_calls, distinct_sources)`` over ``resolves_at > now()``.

        Single SELECT returning both counts so the ``/board`` route
        doesn't pay two round-trips for the footer heartbeat:

        .. code-block:: sql

            SELECT COUNT(*), COUNT(DISTINCT source_id)
            FROM claims
            WHERE resolves_at > now()

        Both columns are non-NULL by construction (``COUNT`` always
        returns a number; ``0`` on an empty match). The defensive
        ``None`` fallback is here so a future query rewrite that
        replaces ``COUNT(*)`` with a subquery / window function can't
        silently NULL one of the values into a ``TypeError`` on
        ``int(None)``.
        """
        now = datetime.now(tz=UTC)
        result = await self._session.execute(
            select(
                func.count(),
                func.count(func.distinct(Claim.source_id)),
            )
            .select_from(Claim)
            .where(Claim.resolves_at > now),
        )
        row = result.one()
        open_calls = int(row[0]) if row[0] is not None else 0
        open_callers = int(row[1]) if row[1] is not None else 0
        return open_calls, open_callers
