"""Scheduler bindings — intentionally empty at v0.

The worker has **no scheduled jobs** at v0. Everything is reactive:

- Resolution: ``POST /claims`` calls ``queue.enqueue_at(t_resolve, ...)``.
  RQ Scheduler holds the job in Redis. No periodic dispatcher; no
  ``pg_cron``; no events-table scan (Q-029).
- Reputation: ``resolve_outcome`` enqueues ``recompute_source_reputation``
  for the affected source the moment the outcome lands. Q-029 proposal.
- Invite expiry: the redemption handler in ``POST /invites/redeem``
  checks ``expires_at`` at consume time. No sweeper.
- Failed-job retries: RQ's built-in ``Retry(max=3, interval=[60, 300, 900])``
  policy + the failed-jobs registry covers MVP §12.1. Operators rerun
  via ``make admin-rerun CLAIM_ID=...``; no sweep job.

This module exists so the conventional ``apps/worker/merlin_worker/schedules``
import path resolves and a Sprint-2 cron primitive can be added here
without breaking the worker's package layout. Until then, it's a
documentation stub.
"""
