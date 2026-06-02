"""``resolve_outcome(claim_id)`` — passive worker job (plan §3).

Fires at ``t_resolve`` (RQ Scheduler held the job). Steps:

1. Load claim + source identity from Postgres.
2. Idempotency guard — abort if ``outcomes`` already has a row for
   this claim (retried RQ jobs must not double-notify the user).
3. Fetch CoinGecko prices at ``t0`` and ``t_resolve`` (±5 min per
   Q-026 rounding policy).
4. Compute observed return + ``is_correct`` via the shared
   ``merlin_core.domain.scoring`` helpers (so the user's
   "you called it right" notification and the leaderboard tally
   agree by construction).
5. INSERT an ``outcomes`` row.
6. Recompute the source's reputation INLINE (Q-029 reactive —
   no daily cron, no queue hop) so the resulting ``weighted_pnl_pct``
   can be threaded into the user notification. The recompute itself
   reads outcomes, writes one ``source_reputation_history`` row, and
   returns the new PnL.
7. Send the user notification via the Telegram Bot API directly,
   including the freshly-recomputed PnL and a "/me · /board" CTA.

On ``AssetNotFoundError`` (CoinGecko 404 between t0 and t_resolve —
Q-023 asset mutation): INSERT an outcome row with
``status='rejected_asset_mutation'`` (auditability + idempotency),
skip the reputation recompute, send the rejection notification.

On any other ``MarketDataError``: raise. RQ's ``Retry(max=3,
interval=[60, 300, 900])`` policy retries 1m / 5m / 15m. After the
final failure, the user is told via the "resolution_failed_final"
template and the job moves to the failed-jobs registry for the
operator's ``make admin-rerun`` path.

Inline-recompute failure recovery: if step 6 raises (DB hiccup,
transient lock contention, anything), the resolver logs and falls
back to the legacy async enqueue path so the snapshot row still
gets written eventually. The user notification then goes out
WITHOUT the PnL chunk (the body still tells them the outcome; the
"/me · /board" CTA still surfaces). This preserves the Sprint-1
post-mortem property: a recompute failure must NEVER drop the
user's outcome notification.

Imports happen lazily inside the function so ``rq``'s pickle path
doesn't choke on the worker's module-level state.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = logging.getLogger("merlin.worker.jobs.resolve_outcome")


def resolve_outcome(claim_id: str) -> None:
    """Sync entry point called by RQ. Bridges to the async coroutine."""
    asyncio.run(_resolve_outcome_async(claim_id))


async def _resolve_outcome_async(claim_id: str) -> None:
    """Run the resolution sequence for ``claim_id``."""
    # Lazy imports keep RQ's pickle-based job-dispatch happy and let
    # the worker boot even when not all modules import cleanly under
    # collection.
    from merlin_core.application.ports import AssetNotFoundError, MarketDataError
    from merlin_core.domain.scoring import is_correct_call
    from merlin_infrastructure.db.repositories import OutcomeRepository
    from merlin_infrastructure.db.session import get_session_factory
    from merlin_infrastructure.market_data.coingecko import CoinGeckoAdapter
    from merlin_infrastructure.notify.telegram.notifier import TelegramNotifier
    from merlin_infrastructure.notify.telegram.templates import (
        rejected_asset_mutation,
        render_outcome,
        resolution_failed_final,
    )

    from merlin_worker.config import Settings

    settings = Settings()
    session_factory = get_session_factory(settings.database_url)

    # Hydrate the context first — if the claim row doesn't exist we
    # abort before doing any I/O, and ``load`` raises the most useful
    # error for the operator to see in the failed-jobs registry.
    async with session_factory() as session:
        claim_ctx = await _ClaimContext.load(session=session, claim_id=claim_id)

        # Idempotency guard. RQ retries call the same job twice; a
        # corrective ``make admin-rerun`` can also re-fire. In both
        # cases we MUST NOT INSERT a duplicate outcome and we MUST
        # NOT re-notify the user.
        outcome_repo = OutcomeRepository(session)
        existing = await outcome_repo.latest_for_claim(claim_ctx.claim_id)
        if existing is not None:
            _log.info(
                "resolve_outcome: outcome already exists for claim %s (status=%s) — skipping",
                claim_id,
                existing.status,
            )
            return

    market = CoinGeckoAdapter(
        api_key=settings.coingecko_api_key or None,
        base_url=settings.coingecko_base_url,
    )
    notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token or "noop",
    )

    try:
        price_t0 = await market.get_price_at(claim_ctx.coingecko_id, claim_ctx.t0)
        price_resolve = await market.get_price_at(
            claim_ctx.coingecko_id,
            claim_ctx.t_resolve,
        )
    except AssetNotFoundError:
        _log.warning(
            "asset disappeared during window for claim %s — Q-023 reject",
            claim_id,
        )
        await _persist_rejected_asset_mutation(
            session_factory=session_factory,
            claim_ctx=claim_ctx,
        )
        await notifier.notify_user(
            claim_ctx.tg_user_id,
            rejected_asset_mutation(asset_symbol=claim_ctx.asset_symbol),
        )
        return
    except MarketDataError:
        # Final retry path — RQ has already retried per Retry(max=3).
        # We only see this exception once RQ has exhausted retries
        # (the retry decorator re-raises so RQ can apply its policy).
        _log.exception("market data failure for claim %s", claim_id)
        await notifier.notify_user(
            claim_ctx.tg_user_id,
            resolution_failed_final(asset_symbol=claim_ctx.asset_symbol),
        )
        raise

    observed_return = (price_resolve.price_usd - price_t0.price_usd) / price_t0.price_usd
    observed_pct = observed_return * 100.0
    is_correct = is_correct_call(claim_ctx.direction, observed_pct)

    # Order matters. The DB write is the only step allowed to crash —
    # RQ retries on raise, and the idempotency guard above
    # short-circuits the retry. Reputation recompute + notification
    # come after the write and each have explicit recovery paths:
    #
    # * Reputation recompute runs INLINE (await ``_recompute_async``)
    #   so we have the fresh PnL to thread into the notification body.
    #   The recompute writes its own append-only snapshot row in a
    #   separate session. If it raises, we log + fall back to the
    #   async enqueue so the snapshot still gets written eventually,
    #   and the notification goes out WITHOUT the PnL chunk.
    # * Notification last. If notification fails (Telegram outage,
    #   user blocked the bot, etc.) a retry won't recover it because
    #   the DB row already exists. Log and move on; the row is enough
    #   for the user to verify via ``/me`` and for the operator to
    #   bridge from logs.
    # * Sprint-1 post-mortem property still holds: a reputation
    #   recompute failure MUST NOT drop the user notification — we
    #   isolate the inline call inside its own try/except, never let
    #   it raise into the surrounding flow.
    await _persist_resolved(
        session_factory=session_factory,
        claim_ctx=claim_ctx,
        observed_return_pct=observed_pct,
        is_correct=is_correct,
        price_t0_usd=price_t0.price_usd,
        price_resolve_usd=price_resolve.price_usd,
        price_t0_ref=price_t0.returned_at.isoformat(),
        price_resolve_ref=price_resolve.returned_at.isoformat(),
    )

    new_pnl_pct = await _inline_recompute_with_async_fallback(
        source_id=claim_ctx.source_id,
        claim_id=claim_id,
    )

    text = render_outcome(
        asset_symbol=claim_ctx.asset_symbol,
        direction=claim_ctx.direction,  # type: ignore[arg-type]
        horizon_seconds=claim_ctx.horizon_seconds,
        pct=observed_pct,
        weighted_pnl_pct=new_pnl_pct,
    )
    try:
        await notifier.notify_user(claim_ctx.tg_user_id, text)
    except Exception:
        _log.exception(
            "resolve_outcome: notify_user failed for claim %s — "
            "outcome row is persisted, user must check /me manually",
            claim_id,
        )


@dataclass(frozen=True)
class _ClaimContext:
    """Lightweight value object holding what the resolver needs.

    Frozen so the resolver can't accidentally mutate the loaded snapshot
    halfway through the sequence — every consumer reads from the same
    immutable dataclass.
    """

    claim_id: UUID
    source_id: str
    tg_user_id: int
    coingecko_id: str
    asset_symbol: str
    direction: str
    horizon_seconds: int
    t0: _dt.datetime
    t_resolve: _dt.datetime

    @classmethod
    async def load(cls, *, session: AsyncSession, claim_id: str) -> _ClaimContext:
        """Hydrate the resolver context from ``claims`` + ``source_identity``.

        One round-trip via ``select(Claim, SourceIdentity.tg_user_id)``
        joined on ``source_id``. Both rows are required for the
        resolver to do its job — a claim with no identity row means
        the source was cascade-deleted between submit and resolve and
        we should let the failure surface for the operator to
        investigate (we don't silently no-op).
        """
        # Local imports keep ``rq``'s pickle dispatch happy.
        from merlin_infrastructure.db.models import Claim, SourceIdentity
        from sqlalchemy import select

        try:
            claim_uuid = UUID(claim_id)
        except ValueError as e:
            raise ValueError(
                f"resolve_outcome got invalid claim_id={claim_id!r}; "
                "expected a UUID string from the API's enqueue path."
            ) from e

        stmt = (
            select(Claim, SourceIdentity.tg_user_id)
            .join(SourceIdentity, SourceIdentity.source_id == Claim.source_id)
            .where(Claim.id == claim_uuid)
        )
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            raise LookupError(
                f"resolve_outcome: no claim row for id={claim_id!r}. "
                "Either the row was never inserted or the source's "
                "identity record was deleted. Operator action required."
            )
        claim, tg_user_id = row

        return cls(
            claim_id=claim.id,
            source_id=claim.source_id,
            tg_user_id=int(tg_user_id),
            coingecko_id=claim.asset_coingecko_id,
            asset_symbol=claim.asset_symbol,
            direction=claim.direction,
            horizon_seconds=claim.horizon_seconds,
            t0=claim.submitted_at,
            t_resolve=claim.resolves_at,
        )


async def _persist_resolved(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    claim_ctx: _ClaimContext,
    observed_return_pct: float,
    is_correct: bool,
    price_t0_usd: float,
    price_resolve_usd: float,
    price_t0_ref: str,
    price_resolve_ref: str,
) -> None:
    """INSERT a happy-path outcome row. Caller-owned TX commits here."""
    from merlin_infrastructure.db.repositories import OutcomeRepository

    async with session_factory() as session:
        repo = OutcomeRepository(session)
        await repo.insert_resolved(
            claim_id=claim_ctx.claim_id,
            source_id=claim_ctx.source_id,
            observed_return_pct=observed_return_pct,
            is_correct=is_correct,
            price_t0_usd=price_t0_usd,
            price_resolve_usd=price_resolve_usd,
            price_t0_ref=price_t0_ref,
            price_resolve_ref=price_resolve_ref,
            resolved_at=_dt.datetime.now(tz=_dt.UTC),
        )
        await session.commit()


async def _persist_rejected_asset_mutation(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    claim_ctx: _ClaimContext,
) -> None:
    """INSERT a Q-023 rejection row. Caller-owned TX commits here."""
    from merlin_infrastructure.db.repositories import OutcomeRepository

    async with session_factory() as session:
        repo = OutcomeRepository(session)
        await repo.insert_rejected_asset_mutation(
            claim_id=claim_ctx.claim_id,
            source_id=claim_ctx.source_id,
            resolved_at=_dt.datetime.now(tz=_dt.UTC),
        )
        await session.commit()


async def _inline_recompute_with_async_fallback(
    *,
    source_id: str,
    claim_id: str,
) -> float | None:
    """Recompute the source's reputation inline; fall back on failure.

    Happy path: ``await _recompute_async(source_id)`` writes the
    snapshot row in its own session and returns the new
    ``weighted_pnl_pct`` (``None`` only on cold-start, which is
    impossible here because we JUST persisted an outcome row for
    this source). The PnL flows into the user notification body.

    Failure path: any exception (DB hiccup, transient lock, the
    recompute module suddenly mis-importing on a hot reload) is
    caught, logged, and triggers the async enqueue fallback so the
    snapshot row eventually lands and the leaderboard catches up by
    the next worker tick. The fallback is itself best-effort —
    losing both paths means the snapshot lags by one outcome until
    the next resolution on this source, which is the same tolerated
    state we had before this code path existed.

    Returns the new PnL, or ``None`` when the inline call failed.
    ``None`` is the signal that downstream callers should render
    the notification WITHOUT the PnL chunk (still includes the CTA).
    """
    # Lazy import keeps RQ's pickle dispatch happy and matches the
    # rest of this module's import strategy.
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    try:
        return await _recompute_async(source_id)
    except Exception:
        _log.exception(
            "resolve_outcome: inline reputation recompute failed for claim %s "
            "(source=%s) — notification will omit PnL; falling back to async enqueue",
            claim_id,
            source_id,
        )
        try:
            _enqueue_reputation_recompute(source_id=source_id)
        except Exception:
            _log.exception(
                "resolve_outcome: async reputation enqueue fallback ALSO failed "
                "for claim %s (source=%s) — leaderboard will lag by one bet "
                "until the next outcome on this source",
                claim_id,
                source_id,
            )
        return None


def _reputation_job_id(source_id: str) -> str:
    """Sanitize ``source_id`` into a job-id RQ will accept.

    ``source_id`` is ``'tg:<32 hex>'`` (``merlin_core.domain.source_id``)
    — the ``:`` makes the literal value illegal as an RQ job_id, which
    only allows ``[A-Za-z0-9_-]``. Replacing ``:`` with ``-`` keeps the
    job_id deterministic (same source → same id, so a re-enqueue
    naturally deduplicates) without leaking any extra surface area.

    Pinning the transformation here (and exercising it in
    ``tests/preflight/test_rq_job_id_format.py``) keeps the "validate
    against a *realistic* source_id, not just a UUID" property under
    CI guard. The Sprint-1 follow-up bug was exactly the preflight
    testing the wrong shape: ``reputation-{uuid4()}`` passed, but
    production produces ``reputation-tg:<hex>`` which RQ rejects.
    """
    return f"reputation-{source_id.replace(':', '-')}"


def _enqueue_reputation_recompute(*, source_id: str) -> None:
    """Enqueue ``recompute_source_reputation`` reactively (Q-029).

    Targets the dedicated ``reputation`` queue so it doesn't compete
    with ``resolve_outcome`` jobs for worker slots. The constant comes
    from ``merlin_infrastructure.queue.names`` — letting this default
    silently to ``"merlin"`` was the original Sprint-1 mismatch bug.
    """
    from merlin_infrastructure.queue.names import REPUTATION_QUEUE_NAME
    from merlin_infrastructure.queue.rq import RQQueueAdapter
    from redis import Redis

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    queue = RQQueueAdapter(
        redis=Redis.from_url(redis_url),
        queue_name=REPUTATION_QUEUE_NAME,
    )
    queue.enqueue(
        "merlin_worker.jobs.recompute_source_reputation",
        source_id,
        job_id=_reputation_job_id(source_id),
    )
