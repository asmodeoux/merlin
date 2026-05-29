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

## 1. DB restore from Fly Postgres snapshot

Reference: MVP §12.8.

When to use: data corruption, accidental drop, ransomware, or a failed
migration we couldn't fix forward.

**TODO Sprint 4:**

- Document the `fly postgres backup list` + `fly postgres backup restore`
  sequence with the exact app slugs.
- Capture the verified RTO (target ≤ 60 min) + RPO (target ≤ 24 h, Fly
  daily snapshots).
- Test a restore on a scratch app and record the result.

## 2. Bot down recovery

Reference: MVP §12.8.

When to use: `merlin-bot` exited or stuck, no Telegram replies.

**TODO Sprint 4:**

- `fly status -a merlin-bot` triage.
- Logs spelunking: `fly logs -a merlin-bot --since 1h | rg ERROR`.
- Restart path: `fly machine restart <id> -a merlin-bot`.
- Token rotation: `fly secrets set TELEGRAM_BOT_TOKEN=...`.
- Webhook-mode-only — verify the `secret_token` round-trip with
  `curl -X POST https://api.telegram.org/bot<TOKEN>/getWebhookInfo`.

## 3. Worker stuck / lag triage

Reference: MVP §12.8.

When to use: resolution backlog, RQ queue depth growing.

**TODO Sprint 4:**

- Inspect: `fly ssh console -a merlin-worker -C "rq info"`.
- Drain a single stuck job: `rq requeue <job_id>`.
- Empty a poison-message queue: `rq empty <queue>` with rationale logged.
- Scale: `fly scale count 2 -a merlin-worker`.

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
