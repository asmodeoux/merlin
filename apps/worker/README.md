# merlin-worker

RQ-based background worker. Runs:

- **Resolution jobs** — fetch CoinGecko price-series points at `t0` and
  `t_resolve`, compute outcome PnL, insert an outcome row (MVP §11.1.D).
  Single market-data source per Q-026.
  No background catalog sync — assets resolve lazily during the bot's
  `/alpha` flow (Q-027) so this
  worker only ever sees CoinGecko ids that already exist in the local
  `assets` table.
- **Reputation recompute** — **reactive, not scheduled** (Q-029 PROPOSED).
  The resolver enqueues `recompute_source_reputation(source_id)` the
  moment a new outcome lands; this worker drains the `reputation` queue
  and appends a `source_reputation_history` row per outcome. The MVP
  spec's previous `pg_cron @ 00:05 UTC` daily job is dropped (Q-029).
- **Retry policy** — RQ's built-in `Retry(max=3, interval=[60,300,900])`
  on every resolution enqueue. Failed jobs land in RQ's failed-job
  registry; operator rerun via `make admin-rerun CLAIM_ID=…`.
- **Direct Telegram notifications** — on outcome write the worker
  POSTs to the Bot API directly (`TelegramNotifier` adapter), no
  detour through the bot service. Bot-voice rules apply to every
  string.

## Forbidden imports

Enforced by `[tool.importlinter]`:

- No `fastapi` — worker is a backend process, not an HTTP server.
- No `telegram` — wrong layer.
- No `merlin_api` / `merlin_bot` / `merlin_client` — the worker talks
  to the DB directly via `merlin_infrastructure.db`, not through the
  API.

## Run

```bash
make dev-up
docker compose logs -f merlin-worker
```

## Debug in IDE

- Local: `⚙️ Debug Worker (local RQ)` (launch.json)
- Docker: `🎯 Attach to Worker (Docker debugpy 5680)` (launch.json)
