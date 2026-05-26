"""Cron-style scheduler bindings.

Resolution-due dispatch is fired by Postgres `pg_cron` (MVP §8.1 — the
daily reputation recompute at 00:05 UTC). Other scheduled work lives
here, dispatched via RQ Scheduler in Sprint 1.

Planned schedules (Sprint 1):

- ``sweep_resolution_failed`` — every 5 minutes. Retries Events stuck
  in ``resolution_failed`` per MVP §12.1.
- ``expire_invites`` — daily 00:00 UTC. Invite TTL sweep.

The asset catalog has **no** scheduled sync — assets materialise lazily
during the bot's `/alpha` flow via ``POST /v1/assets/resolve``
(Q-027).
"""
