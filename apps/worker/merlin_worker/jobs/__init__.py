"""RQ-registered job functions.

Sprint 1 ships:

- `resolve_outcome(claim_id)` — fetch CoinGecko price-series points at
  `t0` and `t_resolve`, compute outcome, insert outcome row. Single
  market-data source per Q-026.
  Append-only — MVP §15 invariant 6 supersedes chain.
- `recompute_source_reputation()` — daily, triggered by pg_cron at
  00:05 UTC. Inserts a new `source_reputation_history` row per source.
- `sweep_resolution_failed()` — MVP §12.1 retry policy.
- `expire_invites()` — invite TTL sweep.

There is **no asset-catalog sync job** at v0. Assets are materialised
lazily by the API (`POST /v1/assets/resolve`) during the bot's `/alpha`
flow — Q-027.
"""
