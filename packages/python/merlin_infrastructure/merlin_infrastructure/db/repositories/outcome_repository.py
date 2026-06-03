"""``OutcomeRepository`` — SQLAlchemy adapter for ``OutcomeRepositoryPort``.

Append-only (MVP §15 invariants 7/8) — exposes ``insert_*`` flavours
only. The supersedes-chain head read (``latest_for_claim``) is used by
the resolver's idempotency guard so a retried RQ job never
double-notifies the user.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from merlin_core.application.ports import (
    OutcomeHistoryEntry,
    OutcomeRecord,
    OutcomeRepositoryPort,
    ResolvedOutcomeForReputation,
    ResolvedOutcomeForTicker,
)
from sqlalchemy import case, exists, func, not_, select
from sqlalchemy.orm import aliased

from merlin_infrastructure.db.models import Claim, Outcome

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _to_record(row: Outcome) -> OutcomeRecord:
    """Project a SQLAlchemy ``Outcome`` row to the layer-crossing record."""
    return OutcomeRecord(
        id=row.id,
        claim_id=row.claim_id,
        source_id=row.source_id,
        status=row.status,
        observed_return_pct=row.observed_return_pct,
        is_correct=row.is_correct,
        price_t0_usd=row.price_t0_usd,
        price_resolve_usd=row.price_resolve_usd,
        price_t0_ref=row.price_t0_ref,
        price_resolve_ref=row.price_resolve_ref,
        supersedes_outcome_id=row.supersedes_outcome_id,
        resolved_at=row.resolved_at,
    )


class OutcomeRepository(OutcomeRepositoryPort):
    """SQLAlchemy adapter for ``OutcomeRepositoryPort``.

    Caller-managed TX: every method flushes; the worker job commits at
    the end of its sequence so the outcome row + the reputation
    follow-up enqueue commit together.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Construct the repository bound to an open ``AsyncSession``."""
        self._session = session

    async def insert_resolved(
        self,
        *,
        claim_id: UUID,
        source_id: str,
        observed_return_pct: float,
        is_correct: bool,
        price_t0_usd: float,
        price_resolve_usd: float,
        price_t0_ref: str,
        price_resolve_ref: str,
        resolved_at: datetime,
    ) -> OutcomeRecord:
        """INSERT a happy-path outcome row."""
        row = Outcome(
            claim_id=claim_id,
            source_id=source_id,
            status="resolved",
            observed_return_pct=observed_return_pct,
            is_correct=is_correct,
            price_t0_usd=price_t0_usd,
            price_resolve_usd=price_resolve_usd,
            price_t0_ref=price_t0_ref,
            price_resolve_ref=price_resolve_ref,
            resolved_at=resolved_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def insert_rejected_asset_mutation(
        self,
        *,
        claim_id: UUID,
        source_id: str,
        resolved_at: datetime,
    ) -> OutcomeRecord:
        """INSERT a Q-023 rejection row.

        ``observed_return_pct`` and ``is_correct`` stay NULL — the
        ``ck_outcomes_resolved_has_return`` /
        ``ck_outcomes_resolved_has_is_correct`` CHECK constraints
        require this. The row exists for auditability; the reputation
        recompute filters on ``status='resolved'`` so the user's hit
        rate is unaffected.
        """
        row = Outcome(
            claim_id=claim_id,
            source_id=source_id,
            status="rejected_asset_mutation",
            observed_return_pct=None,
            is_correct=None,
            resolved_at=resolved_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def latest_for_claim(self, claim_id: UUID) -> OutcomeRecord | None:
        """Head of the supersedes chain.

        v0 we never INSERT a corrective row, so this is equivalent to
        "the most recent outcome for the claim". The implementation is
        written against the chain shape anyway so we don't have to
        revisit it when the operator-rerun path lands.
        """
        result = await self._session.execute(
            select(Outcome)
            .where(Outcome.claim_id == claim_id)
            .order_by(Outcome.resolved_at.desc())
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return None
        return _to_record(row)

    async def count_resolved_and_correct_for_source(
        self,
        source_id: str,
    ) -> tuple[int, int]:
        """Return ``(resolved, correct)`` for the source.

        One round-trip via ``COUNT(*)`` + ``SUM(CASE ... END)``. The
        ``WHERE status='resolved'`` filter drops Q-023 rejection rows
        and ``resolution_failed_final`` rows — those don't count
        against or for the user's reputation. ``is_correct`` is NULL
        on non-resolved rows by CHECK constraint, so the SUM over
        ``CASE WHEN is_correct THEN 1 ELSE 0 END`` is safe to use.

        Lightweight alternative to ``fetch_resolved_for_reputation``
        for callers that only want counts (no PnL math). Kept on the
        port surface for future consumers; the recompute job itself
        uses the richer fetch.
        """
        result = await self._session.execute(
            select(
                func.count().label("resolved"),
                func.coalesce(
                    func.sum(case((Outcome.is_correct.is_(True), 1), else_=0)),
                    0,
                ).label("correct"),
            ).where(
                Outcome.source_id == source_id,
                Outcome.status == "resolved",
            ),
        )
        row = result.one()
        return int(row.resolved), int(row.correct)

    async def fetch_resolved_for_tickers(
        self,
        source_id: str,
    ) -> list[ResolvedOutcomeForTicker]:
        """Per-ticker PnL inputs — same fetch as reputation + ``asset_symbol``.

        Powers ``GET /me/tickers``. The shape matches
        ``fetch_resolved_for_reputation`` (status='resolved' filter,
        outcomes ⋈ claims join) plus the claim's ``asset_symbol``
        column for the application-layer ``groupby`` ahead of the
        scoring pipeline.

        Cold-start sources get an empty list — same contract as the
        reputation fetch.
        """
        result = await self._session.execute(
            select(
                Claim.direction,
                Claim.conviction,
                Claim.asset_symbol,
                Outcome.observed_return_pct,
                Outcome.is_correct,
            )
            .join(Claim, Claim.id == Outcome.claim_id)
            .where(
                Outcome.source_id == source_id,
                Outcome.status == "resolved",
            ),
        )
        rows = result.all()
        return [
            ResolvedOutcomeForTicker(
                direction=str(row.direction),
                conviction=str(row.conviction),
                # Defensive casts mirror ``fetch_resolved_for_reputation``
                # — the CHECK constraints make these non-NULL on the
                # ``status='resolved'`` rows we select, but a future
                # schema relaxation must not silently feed a None into
                # the scoring pipeline.
                observed_return_pct=float(row.observed_return_pct or 0.0),
                is_correct=bool(row.is_correct),
                asset_symbol=str(row.asset_symbol),
            )
            for row in rows
        ]

    async def fetch_history_page(
        self,
        source_id: str,
        *,
        as_of: datetime,
        limit: int,
        offset: int,
    ) -> tuple[list[OutcomeHistoryEntry], int]:
        """Newest-first page + total in one window-function SELECT.

        ``total`` is ``COUNT(*) OVER ()`` — computed within the same
        SELECT as the page rows, so we pay for one round-trip and the
        count is guaranteed consistent with the page (same MVCC
        snapshot, same WHERE).

        Three WHERE filters (port-doc §1-3):

        1. ``status='resolved'`` — Q-023 rejection rows and
           ``resolution_failed_final`` rows are not user-facing.
        2. ``resolved_at <= :as_of`` — caller-frozen window ceiling.
           Pinned by the API route on the first page and
           round-tripped on follow-ups so an outcome resolving
           mid-session can't shift already-paged rows downward
           (which would dup the boundary row on the next click).
        3. ``NOT EXISTS o2 WHERE o2.supersedes_outcome_id = o.id`` —
           MVP §15 invariant 6: only the chain head appears in the
           history list. v0 never INSERTs a corrective row, but the
           filter is here so the user-facing list stays honest the
           day we do.

        Empty page (``offset >= total``) → ``([], total)``. The window
        function returns no rows when the SELECT has no rows, so we
        run a follow-up COUNT-only query on the empty-page branch to
        return an honest total. (The "page has at least one row" path
        is one SELECT; the rare empty-tail path is two — total adds
        one round-trip only when the user paginated past the end.)
        """
        outcome_alias = aliased(Outcome)
        head_predicate = not_(
            exists().where(outcome_alias.supersedes_outcome_id == Outcome.id),
        )

        stmt = (
            select(
                Outcome.claim_id,
                Claim.asset_symbol,
                Claim.direction,
                Claim.horizon_seconds,
                Outcome.observed_return_pct,
                Outcome.is_correct,
                Outcome.resolved_at,
                func.count().over().label("total"),
            )
            .join(Claim, Claim.id == Outcome.claim_id)
            .where(
                Outcome.source_id == source_id,
                Outcome.status == "resolved",
                Outcome.resolved_at <= as_of,
                head_predicate,
            )
            .order_by(Outcome.resolved_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        rows = result.all()
        if not rows:
            # Empty page: window function returned no rows, so we have
            # no ``total`` to read. Run the COUNT-only query (same
            # WHERE shape) so ``has_more`` is honest. The route uses
            # this branch only when the user paginated past the end.
            total_stmt = select(func.count()).select_from(
                select(Outcome.id)
                .where(
                    Outcome.source_id == source_id,
                    Outcome.status == "resolved",
                    Outcome.resolved_at <= as_of,
                    head_predicate,
                )
                .subquery(),
            )
            total_result = await self._session.execute(total_stmt)
            total = int(total_result.scalar() or 0)
            return [], total

        total = int(rows[0].total)
        entries = [
            OutcomeHistoryEntry(
                claim_id=row.claim_id,
                asset_symbol=str(row.asset_symbol),
                direction=str(row.direction),
                horizon_seconds=int(row.horizon_seconds),
                # ``observed_return_pct`` is NOT NULL on resolved rows
                # by CHECK constraint — the ``or 0.0`` is defensive
                # against a future schema relaxation.
                observed_return_pct=float(row.observed_return_pct or 0.0),
                is_correct=bool(row.is_correct),
                resolved_at=row.resolved_at,
            )
            for row in rows
        ]
        return entries, total

    async def fetch_resolved_for_reputation(
        self,
        source_id: str,
    ) -> list[ResolvedOutcomeForReputation]:
        """Per-bet PnL inputs for one source — the recompute's fetch.

        One row per resolved outcome, joined to its claim for the
        per-bet direction + conviction.

        Powers ``recompute_source_reputation``'s conviction-weighted
        PnL math. The query reads ``outcomes`` filtered to
        ``status='resolved'`` (Q-023 rejection rows + retries-exhausted
        rows excluded) and joins ``claims`` on ``claim_id`` for the
        per-bet ``direction`` and ``conviction``.

        Returns an empty list for a cold-start source — the recompute
        treats that as "all numeric fields NULL" by construction.
        """
        result = await self._session.execute(
            select(
                Claim.direction,
                Claim.conviction,
                Outcome.observed_return_pct,
                Outcome.is_correct,
            )
            .join(Claim, Claim.id == Outcome.claim_id)
            .where(
                Outcome.source_id == source_id,
                Outcome.status == "resolved",
            ),
        )
        rows = result.all()
        return [
            ResolvedOutcomeForReputation(
                direction=str(row.direction),
                conviction=str(row.conviction),
                # ``observed_return_pct`` and ``is_correct`` are NOT NULL
                # on resolved outcome rows by CHECK constraint
                # (ck_outcomes_resolved_has_return /
                # ck_outcomes_resolved_has_is_correct). The casts are
                # defensive against a future schema relaxation.
                observed_return_pct=float(row.observed_return_pct or 0.0),
                is_correct=bool(row.is_correct),
            )
            for row in rows
        ]
