# merlin-worker

RQ-based background worker. Runs:

- **Resolution jobs** — fetch CoinGecko price-series points at `t0` and
  `t_resolve`, compute outcome PnL, insert an outcome row (MVP §11.1.D).
  Single market-data source per Q-026.
  No background catalog sync — assets resolve lazily during the bot's
  `/alpha` flow (Q-027) so this
  worker only ever sees CoinGecko ids that already exist in the local
  `assets` table.
- **Reputation recompute** — scheduled by `pg_cron` at 00:05 UTC (MVP
  §8.1, MVP §12); the cron job enqueues an RQ task that this worker
  picks up.
- **Retry sweep** — MVP §12.1 resolution-failed retry policy.
- **Misc background tasks** — invite expiry sweep, ledger reconciliation.

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
