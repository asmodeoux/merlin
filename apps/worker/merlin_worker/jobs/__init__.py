"""RQ-registered job functions.

The worker is fully passive: no periodic scans, no sweeps.
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

Sprint 2 adds periodic jobs, both driven by the RQ ``CronScheduler``
(see ``merlin_worker.schedules``):

- ``sweep_unresolved_claims()`` (Q-030) — the durable resolution
  backstop. Scans Postgres for claims past ``resolves_at + grace`` with
  no outcome row and re-enqueues ``resolve_outcome`` for each. Defends
  against RQ scheduled jobs lost to Redis eviction / worker OOM /
  Redis restart — the failure mode that silently dropped bets before
  Q-030. Idempotent on top of ``resolve_outcome``'s own guard.
- ``send_first_call_nudges()`` (Q-031) — the activation nudge. DMs
  onboarded, named testers who have made zero calls, capped at once a
  week and twice ever per user (limits tracked durably in
  ``engagement_nudges``, never Redis).

Failed jobs (after the 3-retry RQ policy: 60s / 5min / 15min) land in
RQ's failed-jobs registry. An operator runs ``make admin-rerun
CLAIM_ID=...`` (single bet) or ``make admin-backfill`` (every stranded
bet) to retry — both INSERT nothing themselves; they just re-enqueue.
"""

from merlin_worker.jobs.first_call_nudge import send_first_call_nudges
from merlin_worker.jobs.recompute_source_reputation import recompute_source_reputation
from merlin_worker.jobs.resolve_outcome import resolve_outcome
from merlin_worker.jobs.sweep_unresolved import sweep_unresolved_claims

__all__ = [
    "recompute_source_reputation",
    "resolve_outcome",
    "send_first_call_nudges",
    "sweep_unresolved_claims",
]
