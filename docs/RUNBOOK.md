# Merlin Runbook

Operational procedures from when Merlin ran on Fly.io. Sections marked
**Not done** were planned before a wider launch and never written.

## 0. "Is the dev stack alive?" — `make dev-verify`

Use whenever you're not sure if your local environment is functional
(stale containers, env-var drift, port collisions). One command, ≤ 30s,
named failure modes:

```bash
make dev-verify
```

The script (`scripts/dev_verify.sh`) brings up `deploy/docker-compose.dev.yml`,
waits for the postgres + redis healthchecks, polls `http://localhost:8000/health`
+ `/ready`, greps the bot and worker logs for their startup lines, and
prints a clickable URL block. On any failure it dumps the last 50 lines
of the failing service's logs and exits 1.

VS Code: **🚀 Verify Dev Stack (full launch + healthcheck)** task.

After Fly is up, the parallel ritual is "rotate the local bot token":
the prod Telegram bot polls from a Fly machine, so the local stack
**must not share the same `TELEGRAM_BOT_TOKEN`** — only one polling
loop per token is allowed by the Bot API. Register a second bot in
BotFather for dev, drop the token into `apps/{bot,api,worker}/.env`,
and re-run `make dev-up`.

## 1. DB restore from Fly Managed Postgres snapshot

When to use: data corruption, accidental drop, ransomware, or a failed
migration we couldn't fix forward.

**Cluster facts (pinned 2026-06-07):**

- Cluster ID: `zp2wjre68v90dn4q`
- App slug: `merlin-pg` (org `merlin-445`, region `iad`)
- Backups: Fly takes a daily snapshot, 7-day retention. See
  `fly mpg backups list <CLUSTER_ID>` for the snapshot catalogue.

**Recovery commands:**

```bash
# 0. Inventory
fly mpg list                                   # confirm cluster
fly mpg backups list zp2wjre68v90dn4q          # available snapshots

# 1. Restore into a fresh cluster (Fly forbids in-place restore on MPG;
#    creates a new cluster from snapshot and you re-point apps at it)
fly mpg backups restore zp2wjre68v90dn4q \
  --backup-id <id-from-list> \
  --new-cluster-name merlin-pg-rollback

# 2. Detach the broken cluster + attach the new one
fly mpg detach zp2wjre68v90dn4q --app merlin-api
fly mpg detach zp2wjre68v90dn4q --app merlin-worker
fly mpg attach <new-cluster-id> --app merlin-api      # writes DATABASE_URL secret
fly mpg attach <new-cluster-id> --app merlin-worker
deploy/fly/set-secrets.sh                              # rewrites scheme to asyncpg

# 3. Restart machines so they pick up the new DATABASE_URL
fly machine restart -a merlin-api --select all
fly machine restart -a merlin-worker --select all

# 4. Smoke
curl -sf https://merlin-api.fly.dev/ready
```

**Faster path — rebuild from local snapshot:**

```bash
export MPG_RAW_DSN="postgresql://...<new-cluster>..."
make fly-db-restore           # alembic upgrade head + psql data load
```

This is the path used during the initial bootstrap (2026-06-07).
Acceptable only when the data delta between local and prod is small or
when you've just pulled prod down via `make db-pull-from-fly`
beforehand and are restoring what was prod-as-of-pull.

**Not done:**

- Capture the verified RTO (target ≤ 60 min) + RPO (target ≤ 24 h, Fly
  daily snapshots).
- Test a `fly mpg backups restore` end-to-end on a scratch cluster.

## 2. Bot down recovery

When to use: `merlin-tg-bot` (Fly app slug — `merlin-bot` was taken
globally on first provision) exited or stuck, no Telegram replies.

```bash
# 1. Triage
fly status -a merlin-tg-bot
fly logs   -a merlin-tg-bot --since 30m | rg -i 'error|conflict|forbidden'

# 2. Common case: PTB polling claims another instance owns the loop.
#    Only one machine may run with TELEGRAM_MODE=polling per token.
fly scale show  -a merlin-tg-bot                       # should be count=1
fly scale count 1 -a merlin-tg-bot                     # force back to 1 if drifted

# 3. Restart a stuck machine
fly machine list    -a merlin-tg-bot
fly machine restart <id> -a merlin-tg-bot

# 4. Token rotation (BotFather /revoke first, then):
echo "TELEGRAM_BOT_TOKEN=<new>" | fly secrets import -a merlin-tg-bot
echo "TELEGRAM_BOT_TOKEN=<new>" | fly secrets import -a merlin-api      # the API also calls TG (admin notifier)
echo "TELEGRAM_BOT_TOKEN=<new>" | fly secrets import -a merlin-worker   # the worker calls bot API directly
fly machine restart -a merlin-tg-bot --select all
```

When the bot eventually moves to webhook mode (Phase 2), verify the
secret-token round-trip with
`curl -s "https://api.telegram.org/bot$TOKEN/getWebhookInfo"`.

## 3. Worker stuck / lag triage

When to use: resolution backlog, RQ queue depth growing.

```bash
# 1. Triage
fly status -a merlin-worker
fly logs   -a merlin-worker --since 30m | rg -i 'error|timeout|stale'

# 2. Inspect RQ state from inside the container (Redis URL is the only
#    networking the worker needs; rq info reads it from $REDIS_URL).
fly ssh console -a merlin-worker -C "uv run --package merlin-worker rq info"

# 3. Drain or replay
fly ssh console -a merlin-worker -C "uv run --package merlin-worker rq requeue <job_id>"
fly ssh console -a merlin-worker -C "uv run --package merlin-worker rq empty <queue>"   # last resort; log the reason

# 4. Scale horizontally (worker is stateless modulo the DB).
fly scale count 2 -a merlin-worker

# 5. Restart a wedged machine
fly machine list    -a merlin-worker
fly machine restart <id> -a merlin-worker
```

## 4. Unresolved / stranded bets (Q-030 resolution sweep)

When to use: a user reports a bet that expired but never resolved (no
outcome notification), or you suspect RQ scheduled jobs were lost
(Upstash eviction, a worker OOM-SIGKILL, or a Redis restart).

The durable source of truth is Postgres, not Redis: a bet "never
fired" iff its `claims` row is past `resolves_at` and has **no** row in
the append-only `outcomes` table.

**1. Quantify the damage** (connect to prod Postgres — cluster id
pinned in §1):

```bash
fly mpg connect zp2wjre68v90dn4q
```

```sql
-- How many bets silently never resolved
SELECT count(*) AS bets_that_never_fired
FROM claims c
LEFT JOIN outcomes o ON o.claim_id = c.id
WHERE c.resolves_at < now() AND o.id IS NULL;

-- The list, worst-overdue first (the reported bet will be in here)
SELECT c.id AS claim_id, c.asset_symbol, c.direction,
       c.resolves_at, now() - c.resolves_at AS overdue_by,
       c.horizon_seconds
FROM claims c
LEFT JOIN outcomes o ON o.claim_id = c.id
WHERE c.resolves_at < now() AND o.id IS NULL
ORDER BY c.resolves_at;
```

**2. Recover.** The automatic backstop (the Q-030 sweep) re-fires
anything older than the 6 h grace within `RESOLUTION_SWEEP_INTERVAL_S`
(15 min) — and it runs immediately on every worker boot, so a worker
restart forces a catch-up pass. To recover *now* by hand:

```bash
# Every overdue, unresolved bet (runs the sweep once):
fly ssh console -a merlin-worker \
  -C "uv run --package merlin-worker python -m merlin_worker.admin backfill"

# A single specific bet:
fly ssh console -a merlin-worker \
  -C "uv run --package merlin-worker python -m merlin_worker.admin rerun <claim_id>"
```

Both are idempotent: `resolve_outcome` short-circuits if an outcome
already exists, and resolution uses *historical* CoinGecko prices at
the claim's original `t0` / `t_resolve`, so a late re-fire is still
correct — just delayed. Locally the same targets are `make
admin-backfill` and `make admin-rerun CLAIM_ID=<id>`.

**3. Stop the bleeding at the source (eviction).** The root cause is
the prod Upstash Redis being created with `--enable-eviction`
(`deploy/fly/README.md`): it is allowed to drop the long-lived
`rq:scheduled` / `rq:job` keys under memory pressure. The sweep makes
us resilient to this, but eviction should still be **disabled** so the
queue isn't fighting the bot's PTB persistence (co-tenant in the same
Upstash DB 0) for memory. This is NOT doable via `flyctl` (`fly redis
update` only changes plan / replica regions). Disable it via:

- **Upstash console** — open the `merlin-redis` database (Fly
  dashboard → Upstash integration), Configuration → Eviction →
  Disable; or
- **Upstash Developer API**:
  `curl -u "$UPSTASH_EMAIL:$UPSTASH_API_KEY" -X POST https://api.upstash.com/v2/redis/disable-eviction/<database_id>`

With eviction off, a full Redis *rejects writes* (loud — a failed
`enqueue` rolls the claim back at submit, no silent loss) instead of
dropping pending work. If you hit that ceiling, bump the Redis plan or
split RQ onto its own Upstash instance (separate from PTB persistence).

**4. Resolution-failed (retries exhausted).** Distinct from the above:
bets that DID fire but failed every RQ retry land an `outcomes` row
with `status='resolution_failed_final'`. RQ's built-in
`Retry(max=3, interval=[60,300,900])` handles the automatic retries;
re-run the human path with `make admin-rerun CLAIM_ID=<id>` once the
upstream issue (e.g. CoinGecko outage) clears.

## 5. Override audit trail walkthrough

When to use: a user disputes an outcome; need to inspect override history.

**Not done:**

- `SELECT * FROM override_log WHERE source_id = $1 ORDER BY created_at;`
  — append-only.
- Cross-reference with `attribution_ledger` for invite-credit changes.
- Walk through the exact admin Telegram command that produced each row.

## 6. Backup / DR posture (v0)

- **Daily** — Fly Postgres automatic snapshots, 7-day retention.
- **Off-Fly external backup** — explicitly **deferred to Phase 2**. v0
  accepts loss of up to one Fly region's worth of data in the
  worst-case-Fly-outage scenario.
- **Restore drill cadence** — never set.

**Not done:**

- Run a restore drill against a scratch app.
- Document the verified restore time in the table above.
- Pick a cadence (monthly? quarterly?) and add to the team's recurring
  ops calendar.
