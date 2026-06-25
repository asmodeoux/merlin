"""Scheduler bindings — one recurring job (Q-030).

The worker is still mostly reactive (Q-029):

- Reputation: ``resolve_outcome`` enqueues ``recompute_source_reputation``
  for the affected source the moment the outcome lands.
- Invite expiry: the redemption handler in ``POST /invites/redeem``
  checks ``expires_at`` at consume time. No sweeper.
- Failed-job retries: RQ's built-in ``Retry(max=3, interval=[60, 300, 900])``
  policy + the failed-jobs registry covers the automatic-retry half of
  MVP §12.1.

But resolution is NOT purely reactive anymore. ``POST /claims`` still
schedules the resolver with ``queue.enqueue_at(t_resolve, ...)`` —
that job lives only in Redis, parked for up to the 90-day max horizon.
Prod Redis (Upstash, eviction-enabled) is allowed to drop those
``rq:scheduled`` / ``rq:job`` keys under memory pressure, and a worker
OOM-SIGKILL or a Redis restart can lose them too. When that happens
the bet's ``resolves_at`` arrives and nothing fires — silent data loss,
because Q-029 had removed the MVP §12.1 stale-job sweep.

``cron.py`` hosts the worker's RQ 2.5+ ``CronScheduler``, driven from a
daemon thread the worker starts on boot (``merlin_worker.main``). Each
job is gated by its own ``*_enabled`` flag; the thread starts iff at
least one is registered. Two jobs at v0:

- ``sweep_unresolved_claims`` (ADR (v) / Q-030) — reinstates the
  MVP §12.1 stale-job sweep. Reconciles the durable ``claims`` table
  against the queue and re-fires anything stranded by a lost Redis
  job. Operators can also run it on demand via ``make admin-backfill``
  (all overdue) or ``make admin-rerun CLAIM_ID=...`` (one bet).
- ``send_first_call_nudges`` (ADR (w) / Q-031) — DMs onboarded, named
  testers with zero calls, capped at once a week / twice ever per user
  (limits in the durable ``engagement_nudges`` table).
"""
