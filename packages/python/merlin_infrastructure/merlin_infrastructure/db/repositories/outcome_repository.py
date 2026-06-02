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
    OutcomeRecord,
    OutcomeRepositoryPort,
    ResolvedOutcomeForReputation,
)
from sqlalchemy import case, func, select

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
