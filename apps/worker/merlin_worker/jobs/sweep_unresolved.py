"""``sweep_unresolved_claims()`` — durable resolution backstop (Q-030).

The ``POST /claims`` path schedules resolution as a single RQ
``enqueue_at`` job parked in Redis for the whole horizon (up to 90
days). That Redis key is the *only* record of the pending resolution
the scheduler ever consults — and prod Redis (Upstash, eviction
enabled) is explicitly allowed to drop keys under memory pressure.
When the ``rq:scheduled`` / ``rq:job`` key is evicted (or the worker
OOM-SIGKILLs mid-job, or Redis restarts without persistence), the
bet's ``resolves_at`` arrives and nothing fires. The user never gets
an outcome and nothing notices, because Q-029 removed the MVP §12.1
stale-job sweep.

This job is that sweep, reinstated (Q-030). It periodically asks the
durable source of truth — the ``claims`` table in Postgres — for bets
that are well past due (``resolves_at`` older than the configured
grace) but still have no ``outcomes`` row, and re-enqueues
``resolve_outcome`` for each. It is safe to run as often as you like:

* The enqueue reuses the deterministic ``resolve-{claim_id}`` job id,
  so a second sweep before the first re-enqueue drains just collapses
  onto the same job id.
* ``resolve_outcome`` is itself idempotent — it short-circuits when an
  outcome row already exists — so a re-enqueue that races a real
  resolution lands as a no-op.
* Resolution reads *historical* CoinGecko prices at the claim's
  original ``t0`` / ``t_resolve``, so a late resolution is still
  correct, just delayed.

Driven by the RQ ``CronScheduler`` in
``merlin_worker.schedules.cron`` (started by the worker on boot). The
``CronScheduler`` enqueues interval jobs immediately on start, so a
worker restart doubles as a free catch-up pass after any outage.

Imports happen lazily inside the function so ``rq``'s pickle-based job
dispatch doesn't choke on the worker's module-level state — same
discipline as ``resolve_outcome``.
"""

from __future__ import annotations

import asyncio
import logging
import os

_log = logging.getLogger("merlin.worker.jobs.sweep_unresolved")


def sweep_unresolved_claims() -> int:
    """Sync entry point called by RQ. Returns the re-enqueued count."""
    return asyncio.run(run_sweep())


async def run_sweep(
    *,
    grace_seconds: int | None = None,
    max_batch: int | None = None,
) -> int:
    """Find overdue, unresolved claims and re-enqueue their resolution.

    ``grace_seconds`` / ``max_batch`` default to the worker Settings
    (``resolution_sweep_grace_s`` / ``resolution_sweep_max_batch``).
    The ``make admin-backfill`` path passes explicit overrides so an
    operator can, e.g., drop the grace to 0 for an emergency drain.
    Returns the number of resolution jobs re-enqueued.
    """
    from merlin_infrastructure.db.repositories import ClaimRepository
    from merlin_infrastructure.db.session import get_session_factory
    from merlin_infrastructure.queue.names import RESOLUTION_QUEUE_NAME
    from merlin_infrastructure.queue.rq import RQQueueAdapter
    from redis import Redis

    from merlin_worker.config import Settings

    settings = Settings()
    grace = settings.resolution_sweep_grace_s if grace_seconds is None else grace_seconds
    batch = settings.resolution_sweep_max_batch if max_batch is None else max_batch

    session_factory = get_session_factory(settings.database_url)
    async with session_factory() as session:
        repo = ClaimRepository(session)
        claim_ids = await repo.list_unresolved_overdue(
            grace_seconds=grace,
            limit=batch,
        )

    if not claim_ids:
        _log.info("resolution sweep: no overdue unresolved claims")
        return 0

    # Re-enqueue onto the SAME resolution queue the API enqueues on —
    # the constant keeps producer + worker drained-queues aligned
    # (the original Sprint-1 "merlin" mismatch bug). Immediate
    # ``enqueue`` (not ``enqueue_at``): these bets are already overdue,
    # so they should fire on the next free worker slot.
    redis_url = os.environ.get("REDIS_URL", settings.redis_url)
    queue = RQQueueAdapter(
        redis=Redis.from_url(redis_url),
        queue_name=RESOLUTION_QUEUE_NAME,
    )
    for claim_id in claim_ids:
        queue.enqueue(
            "merlin_worker.jobs.resolve_outcome",
            str(claim_id),
            job_id=f"resolve-{claim_id}",
        )

    # WARNING level on purpose: a non-zero sweep means jobs went
    # missing from Redis. That's a signal worth surfacing in the logs /
    # Sentry breadcrumbs, not a routine INFO. A healthy stack sweeps to
    # zero every tick.
    _log.warning(
        "resolution sweep: re-enqueued %d overdue unresolved claim(s)",
        len(claim_ids),
    )
    return len(claim_ids)
