"""``resolve_outcome(claim_id)`` — passive worker job (plan §3).

Fires at ``t_resolve`` (RQ Scheduler held the job). Steps:

1. Load claim + event + asset rows from Postgres.
2. Idempotency guard — abort if ``event.status != 'pending'``.
3. Fetch CoinGecko prices at ``t0`` and ``t_resolve`` (±5 min per
   Q-026 rounding policy).
4. Compute observed return + PnL-if-followed (formula stub).
5. INSERT ``outcomes`` row.
6. UPDATE ``events.status = 'resolved'``.
7. Enqueue ``recompute_source_reputation(source_id)`` — reactive
   reputation (Q-029 proposal). No daily cron.
8. Send the user notification via the Telegram Bot API directly
   (one less hop than relaying through the bot service).

On ``AssetNotFoundError`` (CoinGecko 404 between t0 and t_resolve —
Q-023 asset mutation): UPDATE ``events.status = 'rejected_asset_mutation'``,
skip the outcome row, send the rejection notification.

On any other ``MarketDataError``: raise. RQ's ``Retry(max=3,
interval=[60, 300, 900])`` policy retries 1m / 5m / 15m. After the
final failure, the user is told via the "resolution_failed_final"
template and the job moves to the failed-jobs registry for the
operator's ``make admin-rerun`` path.

Imports happen lazily inside the function so ``rq``'s pickle path
doesn't choke on the worker's module-level state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_log = logging.getLogger("merlin.worker.jobs.resolve_outcome")


def resolve_outcome(claim_id: str) -> None:
    """Sync entry point called by RQ. Bridges to the async coroutine."""
    asyncio.run(_resolve_outcome_async(claim_id))


async def _resolve_outcome_async(claim_id: str) -> None:
    """Run the eight-step resolution sequence for ``claim_id``."""
    # Lazy imports keep RQ's pickle-based job-dispatch happy and let
    # the worker boot when alembic models haven't all landed yet.
    from merlin_core.application.ports import AssetNotFoundError, MarketDataError
    from merlin_infrastructure.market_data.coingecko import CoinGeckoAdapter
    from merlin_infrastructure.notify.telegram.notifier import TelegramNotifier
    from merlin_infrastructure.notify.telegram.templates import (
        rejected_asset_mutation,
        render_outcome,
        resolution_failed_final,
    )

    from merlin_worker.config import Settings

    settings = Settings()

    # Sprint 1: load claim + event + asset from the DB. Until then the
    # job is a thin scaffold that exercises the wire path — RQ enqueue
    # → worker dispatch → market-data lookup → notifier — so the
    # integration test layer can assert the contract.
    claim_ctx = _ClaimContext.load(claim_id)

    market = CoinGeckoAdapter(
        api_key=settings.coingecko_api_key or None,
        base_url=settings.coingecko_base_url,
    )
    notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token or "noop",
    )

    try:
        price_t0 = await market.get_price_at(claim_ctx.coingecko_id, claim_ctx.t0)
        price_resolve = await market.get_price_at(claim_ctx.coingecko_id, claim_ctx.t_resolve)
    except AssetNotFoundError:
        _log.warning(
            "asset disappeared during window for claim %s — Q-023 reject",
            claim_id,
        )
        # Sprint 1: UPDATE events.status = 'rejected_asset_mutation'.
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

    # Sprint 1: INSERT outcomes + UPDATE events.status='resolved' +
    # enqueue recompute_source_reputation (Q-029 reactive reputation).
    _persist_outcome(
        claim_ctx=claim_ctx,
        observed_return=observed_return,
        price_t0_ref=price_t0.returned_at.isoformat(),
        price_resolve_ref=price_resolve.returned_at.isoformat(),
    )
    _enqueue_reputation_recompute(source_id=claim_ctx.source_id)

    text = render_outcome(
        asset_symbol=claim_ctx.asset_symbol,
        direction=claim_ctx.direction,  # type: ignore[arg-type]
        horizon=claim_ctx.horizon,  # type: ignore[arg-type]
        pct=observed_pct,
    )
    await notifier.notify_user(claim_ctx.tg_user_id, text)


class _ClaimContext:
    """Lightweight value object holding what the resolver needs.

    Sprint 1 wires ``load`` against the real Postgres tables (claims,
    events, assets, source_identity). For now it raises so the job
    is structurally complete but advertises the missing wiring loudly.
    """

    def __init__(
        self,
        *,
        claim_id: str,
        source_id: str,
        tg_user_id: int,
        coingecko_id: str,
        asset_symbol: str,
        direction: str,
        horizon: str,
        t0: _dt.datetime,
        t_resolve: _dt.datetime,
        event_id: str,
    ) -> None:
        self.claim_id = claim_id
        self.source_id = source_id
        self.tg_user_id = tg_user_id
        self.coingecko_id = coingecko_id
        self.asset_symbol = asset_symbol
        self.direction = direction
        self.horizon = horizon
        self.t0 = t0
        self.t_resolve = t_resolve
        self.event_id = event_id

    @classmethod
    def load(cls, claim_id: str) -> _ClaimContext:
        """Hydrate the context from the DB. Sprint 1 wires this against real tables."""
        raise NotImplementedError(
            "resolve_outcome.load is wired against the DB in Sprint 1; "
            "integration tests pass a fake claim context via monkeypatching."
        )


def _persist_outcome(
    *,
    claim_ctx: _ClaimContext,
    observed_return: float,
    price_t0_ref: str,
    price_resolve_ref: str,
) -> None:
    """INSERT the outcome row + UPDATE event status. Sprint 1 wiring."""
    raise NotImplementedError(
        "outcomes / events writes land in Sprint 1 with the SQLAlchemy session"
    )


def _enqueue_reputation_recompute(*, source_id: str) -> None:
    """Enqueue ``recompute_source_reputation`` reactively (Q-029)."""
    import os

    from merlin_infrastructure.queue.rq import RQQueueAdapter
    from redis import Redis

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    queue = RQQueueAdapter(redis=Redis.from_url(redis_url))
    queue.enqueue(
        "merlin_worker.jobs.recompute_source_reputation",
        source_id,
        job_id=f"reputation:{source_id}",
    )


# datetime alias kept local to satisfy the forward-ref in _ClaimContext.
import datetime as _dt  # noqa: E402
