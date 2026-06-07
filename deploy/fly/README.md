# deploy/fly/

Fly.io deployment helpers. Keeps the per-app `fly secrets`, the
local-to-Fly DB seed, and the reverse Fly-to-local pull behind three
shell scripts so the wiring is auditable and re-runnable.

## Fly resources (`merlin-445` org)

| Kind                    | Name             | Created via                                                    |
| ----------------------- | ---------------- | --------------------------------------------------------------- |
| App (FastAPI)           | `merlin-api`     | `fly apps create merlin-api --org merlin-445`                  |
| App (PTB polling bot)   | `merlin-tg-bot`  | `fly apps create merlin-tg-bot --org merlin-445` (the bare `merlin-bot` name was already taken globally on 2026-06-07) |
| App (RQ worker)         | `merlin-worker`  | `fly apps create merlin-worker --org merlin-445`               |
| Managed Postgres        | `merlin-pg`      | `fly mpg create --name merlin-pg --org merlin-445 --region iad --plan Basic --pg-major-version 16` |
| Upstash Redis           | `merlin-redis`   | `fly redis create --name merlin-redis --org merlin-445 --region iad --no-replicas --enable-eviction` |

The Postgres cluster is attached to `merlin-api` and `merlin-worker`
(`fly mpg attach <cluster-id> --app <app>`). The bot does NOT get
`DATABASE_URL` — it talks to the API over Flycast.

> **pgvector**: Fly MPG exposes only `schema_admin` (no superuser), so
> `CREATE EXTENSION vector` is rejected. v0 doesn't use pgvector
> (Phase 2 graph features will). When that lands, file a Fly support
> ticket to enable `vector` on the cluster — or migrate the cluster to
> Supabase/Neon (the only secret change is `DATABASE_URL`).

## Scripts

### `set-secrets.sh`

One-shot push of secrets to Fly for all three apps. Reads each
`apps/<svc>/.env` (kept gitignored), applies three classes of override,
and stages + deploys every value via `fly secrets set --stage` /
`fly secrets deploy`.

Required inputs (env vars — never written to disk except for the HMAC
token):

```bash
export MPG_RAW_DSN="postgresql://fly-user:<pwd>@pgbouncer.<cluster>.flympg.net/fly-db"  # from `fly mpg attach`
export REDIS_RAW_URL="redis://default:<pwd>@fly-merlin-redis.upstash.io:6379"          # from `fly redis create`

make fly-secrets-sync
```

Overrides applied per app:

| App             | Key                        | Source                              |
| --------------- | -------------------------- | ----------------------------------- |
| api, worker     | `DATABASE_URL`             | `MPG_RAW_DSN` w/ scheme rewritten to `postgresql+asyncpg://` |
| all three       | `REDIS_URL`                | `REDIS_RAW_URL` verbatim. Upstash only supports DB 0 (`SELECT 1` errors); RQ (`rq:*`) and PTB persistence (`merlin_bot:ptb:*`) co-tenant in the single DB via disjoint key prefixes. |
| bot             | `MERLIN_API_BASE_URL`      | `http://merlin-api.internal:8000` (private 6PN direct to uvicorn — Flycast routes via the public-side proxy on 80/443, so `.flycast:8000` would not connect). |
| all three       | `MERLIN_API_INTERNAL_TOKEN`| `openssl rand -hex 32`, cached at `deploy/fly/.internal-token` (gitignored) so subsequent runs reuse the value |
| all three       | `ENVIRONMENT`              | `prod` (overrides the `dev` in `.env`) |

Everything else (TG token, OPENROUTER/COINGECKO/SENTRY keys, LLM model
slugs, RQ queue config) comes verbatim from the corresponding `.env`.

### `db-restore-from-local.sh` (`make fly-db-restore`)

One-shot: `pg_dump` the local docker compose Postgres (host:5433) and
`pg_restore` into Fly Managed Postgres over a `fly proxy` tunnel.

Requires `MPG_RAW_DSN` and `pg_dump`/`pg_restore` v16+.

### `db-pull-to-local.sh` (`make db-pull-from-fly`)

The reverse — pulls a Fly Managed Postgres snapshot into the local
docker compose volume. Used for reproducing prod bugs locally. Wipes
the local stack first (compose down) so you never end up with a
half-merged state.

## Workflow on a fresh checkout

```bash
# 1. Pre-flight
fly auth login
brew install postgresql@16      # only required for the DB scripts below
export MPG_RAW_DSN="postgresql://..."   # from `fly mpg attach` output
export REDIS_RAW_URL="redis://..."      # from `fly redis create` output

# 2. Push secrets to Fly
make fly-secrets-sync

# 3. (First-time only) seed Fly DB from local
make dev-down                   # release the prod TG token's polling lease
make fly-db-restore

# 4. Deploy
make fly-deploy-all             # api -> worker -> bot; api's release_command runs alembic
fly scale count 1 -a merlin-tg-bot  # single poller, hard rule

# 5. Verify
curl -sf https://merlin-api.fly.dev/health
curl -sf https://merlin-api.fly.dev/ready
# /start in Telegram against the prod bot @handle.
```

## Local dev parallelism

After Fly is verified, swap the prod TG token in `apps/{bot,api,worker}/.env`
for the dev bot's BotFather token. From that point on, `make dev-up`
and Fly run in parallel — different tokens mean both polling loops are
fine. See `docs/RUNBOOK.md` §0 for the rotation checklist.
