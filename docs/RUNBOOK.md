# Merlin Runbook

Operational procedures for the on-call engineer. **Stub — Sprint 4
fills the procedures with real commands and verified recovery times
pre-launch.** Each section header below maps to a MVP §12 scenario; the
TODO callouts list the gaps Sprint 4 must close.

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

Reference: MVP §12.8.

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

**TODO Sprint 4:**

- Capture the verified RTO (target ≤ 60 min) + RPO (target ≤ 24 h, Fly
  daily snapshots).
- Test a `fly mpg backups restore` end-to-end on a scratch cluster.

## 2. Bot down recovery

Reference: MVP §12.8.

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
echo "TELEGRAM_BOT_TOKEN=<new>" | fly secrets import -a merlin-worker   # the worker calls bot API directly per plan §3
fly machine restart -a merlin-tg-bot --select all
```

When the bot eventually moves to webhook mode (Phase 2), verify the
secret-token round-trip with
`curl -s "https://api.telegram.org/bot$TOKEN/getWebhookInfo"`.

## 3. Worker stuck / lag triage

Reference: MVP §12.8.

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

## 4. Resolution-failed sweep

Reference: MVP §12.1 (retry policy).

When to use: outcomes marked `resolution_failed` after the automatic
retry window.

**TODO Sprint 4:**

- Query the outstanding set:
  `SELECT * FROM events WHERE status = 'resolution_failed' AND t_resolve < now() - interval '24 hours';`
- Manual re-enqueue (post-RQ-Retry exhaustion):
  `make admin-rerun CLAIM_ID=<id>` — single one-shot enqueue, no sweeper.
- Document the criteria for permanent failure (admin override required).
- Note: there is no periodic "resolution-failed sweep" worker (Q-029
  PROPOSED). RQ's built-in `Retry(max=3, interval=[60,300,900])` handles
  the automatic retries; this section is for the human-driven rerun path
  after those exhaust.

## 5. Override audit trail walkthrough

Reference: MVP §12.

When to use: a user disputes an outcome; need to inspect override history.

**TODO Sprint 4:**

- `SELECT * FROM override_log WHERE source_id = $1 ORDER BY created_at;`
  — append-only.
- Cross-reference with `attribution_ledger` for invite-credit changes.
- Walk through the exact admin Telegram command that produced each row.

## 6. Backup / DR posture (v0)

Reference: MVP §12.8.

- **Daily** — Fly Postgres automatic snapshots, 7-day retention.
- **Off-Fly external backup** — explicitly **deferred to Phase 2**. v0
  accepts loss of up to one Fly region's worth of data in the
  worst-case-Fly-outage scenario.
- **Restore drill cadence** — Sprint 4 establishes; pre-launch sets
  the cadence.

**TODO Sprint 4:**

- Run a restore drill against a scratch app.
- Document the verified restore time in the table above.
- Pick a cadence (monthly? quarterly?) and add to the team's recurring
  ops calendar.

## 7. Incident communication

**TODO Sprint 4:**

- Internal channel name + escalation list.
- User-facing comms template (Telegram /announcement command).
- Postmortem template + storage location.
