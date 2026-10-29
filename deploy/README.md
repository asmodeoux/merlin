# deploy/

Deployment + local-compose assets.

## `postgres-dev/` — custom Postgres dev image

`pgvector/pgvector:pg16` only. Q-029
drops `pg_cron` from the v0 stack (Q-029): scheduling is RQ Scheduler
`enqueue_at`, reputation recompute is reactive on outcome write. No
extension other than `vector` is required.

`vector` is enabled on first start via
`init.d/01-enable-extensions.sh` (idempotent `CREATE EXTENSION IF NOT
EXISTS`).

## `docker-compose.dev.yml` — local dev

`make dev-up` runs:

- `postgres` — custom image, port 5432
- `redis` — `redis:7-alpine`, port 6379
- `merlin-api` — `dev` target, hot reload + debugpy on **5678**
- `merlin-bot` — `dev` target, polling + debugpy on **5679**
- `merlin-worker` — `dev` target, RQ verbose + debugpy on **5680**

Source for `apps/` and `packages/` is volume-mounted; edits land
without rebuilds. `.env.example` per app is the `env_file:` (override
with your own `.env` next to the `.env.example` when you need real
secrets).

## `docker-compose.test.yml` — prod image build check

`make docker-test` builds the **prod** target for api/bot/worker so a
broken Dockerfile fails fast. The integration tests run in the main CI
workflow, where they start their own Postgres via testcontainers.

CI hooks: `.github/workflows/docker-test.yml`.

## Fly.io deploys

Each app ships its own `fly.toml` (`apps/<svc>/fly.toml`). Three Fly
apps in the `merlin-445` org:

| Local service | Fly app slug    | Notes                                                                      |
| ------------- | --------------- | -------------------------------------------------------------------------- |
| `merlin-api`  | `merlin-api`    | `release_command` runs `alembic upgrade head` before traffic flips.        |
| `merlin-bot`  | `merlin-tg-bot` | The bare `merlin-bot` name was taken globally on 2026-06-07; slug differs. |
| `merlin-worker` | `merlin-worker` | Stateless modulo the DB; safe to scale horizontally.                     |

Order matters when deploying all three — `merlin-api` first so its
`release_command` migrates the schema before bot/worker boot against
it. The Makefile encodes the right order:

```bash
make fly-deploy-all              # api -> worker -> bot
make fly-deploy-api              # individual; same as above for one app
```

Or in VS Code: **🚀 Deploy: API / 🤖 Deploy: Bot / ⚙️ Deploy: Worker /
🚀 Deploy: All** tasks.

### Secrets: the `.env`-once-then-Fly-secrets convention

Local dev keeps everything in gitignored `apps/<svc>/.env`. Fly never
sees that file directly — `deploy/fly/set-secrets.sh` reads each `.env`,
applies three classes of override, and pushes via `fly secrets import
--stage`:

| App           | Override                                                                                                |
| ------------- | ------------------------------------------------------------------------------------------------------- |
| api, worker   | `DATABASE_URL` rewritten to `postgresql+asyncpg://...` (MPG attach gives a sync-driver DSN).            |
| bot           | `MERLIN_API_BASE_URL=http://merlin-api.internal:8000` (private 6PN direct to uvicorn; Flycast routes via the public-side proxy on 80/443, so `.flycast:8000` would not connect). |
| all three     | `MERLIN_API_INTERNAL_TOKEN` generated once via `openssl rand -hex 32`, cached in `deploy/fly/.internal-token` (gitignored) so the three apps share one HMAC value. |
| all three     | `REDIS_URL` from Upstash. Upstash only supports DB 0 (`SELECT 1` errors); RQ (`rq:*`) and PTB (`merlin_bot:ptb:*`) co-tenant via disjoint key prefixes. |
| all three     | `ENVIRONMENT=prod`, `LOG_LEVEL=INFO`.                                                                   |

After the initial verification, rotate the dev `.env` files to a
separate BotFather token (production and local can't share the same
polling token). See `deploy/fly/README.md` for the full workflow.

### Database — Fly Managed Postgres

Cluster `merlin-pg` (id `zp2wjre68v90dn4q`, region `iad`, basic plan).
Attached to `merlin-api` and `merlin-worker` via `fly mpg attach`. The
bot does **not** get `DATABASE_URL` — it talks to the API only.

> pgvector is not installed: Fly MPG only exposes the `schema_admin`
> role and `CREATE EXTENSION vector` requires superuser. v0 doesn't
> use vector columns; revisit when Phase 2 embeddings land.

To seed Fly with the local docker dev DB:

```bash
export MPG_RAW_DSN="postgresql://...<from `fly mpg attach` output>..."
make fly-db-restore                  # alembic + psql data load (idempotent)
```

Reverse direction (pull prod into local for debugging):

```bash
make db-pull-from-fly
```

### Redis — Upstash via Fly Extensions

`merlin-redis` (Upstash). Upstash only supports the default logical
DB 0 — all three apps share it. Key prefixes keep workloads disjoint:
`rq:*` (worker, api enqueues) and `merlin_bot:ptb:*` (PTB
persistence). Eviction enabled, no replicas (v0). Cost: $0.20 per 100K
commands, no fixed monthly fee.
