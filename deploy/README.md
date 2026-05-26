# deploy/

Deployment + local-compose assets.

## `postgres-dev/` — custom Postgres dev image

`pgvector/pgvector:pg16` + `pg_cron`. MVP §8.1 requires `pg_cron` for
the daily reputation recompute at 00:05 UTC, but the upstream pgvector
image doesn't ship it.

Both extensions are enabled on first start via
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

## `docker-compose.test.yml` — integration suite against prod images

`make docker-test` builds the **prod** target for api/bot/worker, brings
them up, and runs `pytest -m integration` in a `runner` sidecar. The
runner's exit code is the compose exit code (`--exit-code-from runner`).

CI hooks: `.github/workflows/docker-test.yml`.

## Fly.io deploys

Each app ships its own `fly.toml` (`apps/<svc>/fly.toml`). Phase 0 is
deploy-on-tag; Phase 1 wires per-PR preview machines.

```bash
fly deploy --config apps/api/fly.toml --app merlin-api
fly deploy --config apps/bot/fly.toml --app merlin-bot
fly deploy --config apps/worker/fly.toml --app merlin-worker
```

Secrets are set via `fly secrets set KEY=value` (one per env var) per
app — never checked into the repo.
