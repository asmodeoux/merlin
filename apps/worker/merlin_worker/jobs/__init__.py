"""RQ-registered job functions.

The worker is fully passive (plan §3): no periodic scans, no sweeps.
Every job here fires because something specific happened — a claim
committed (resolve), or an outcome landed (recompute reputation).

Sprint 1 ships:

- ``resolve_outcome(claim_id)`` — scheduled by ``POST /claims`` for
  ``t_resolve``. Fetches CoinGecko price-series points at ``t0`` and
  ``t_resolve``, computes outcome, INSERTs ``outcomes``, UPDATEs
  ``events.status``, enqueues ``recompute_source_reputation``, and
  sends the user notification directly via the Telegram Bot API.
  Single market-data source per Q-026. Append-only per MVP §15 invariant 6 supersedes chain.
- ``recompute_source_reputation(source_id)`` — reactive (Q-029
  proposal): enqueued by ``resolve_outcome`` the moment an outcome
  lands. INSERTs one new ``source_reputation_history`` row per
  outcome. No daily cron, no ``pg_cron`` extension (Q-029).

There is **no asset-catalog sync job** at v0. Assets are materialised
lazily by the API (``POST /assets/resolve``) during the bot's
``/alpha`` flow — Q-027.

Failed jobs (after the 3-retry RQ policy: 60s / 5min / 15min) land in
RQ's failed-jobs registry. An operator runs ``make admin-rerun
CLAIM_ID=...`` to retry. No periodic sweeper.
"""

from merlin_worker.jobs.recompute_source_reputation import recompute_source_reputation
from merlin_worker.jobs.resolve_outcome import resolve_outcome

__all__ = ["recompute_source_reputation", "resolve_outcome"]
