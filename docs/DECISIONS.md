# Decision log

Short records of the product and architecture decisions referenced as
`Q-NNN` across the code and docs. Implementation-level decisions are in
the ADR table in [`ARCHITECTURE.md`](./ARCHITECTURE.md) §14.

## Resolved

| ID | Decision |
| --- | --- |
| Q-R01 | **Regret is two-sided.** Every claim gets both `pnl_if_followed` and `pnl_if_ignored`; `regret = pnl_if_followed - pnl_if_ignored`. Every source is scorable from its first claim, without committing capital. |
| Q-R02 | **Outcomes are continuous PnL**, not a win/loss enum. The sign of `pnl_if_followed` carries win/loss. |
| Q-R03 | **Horizons extend to 90 days** (`30d`, `90d` added) so multi-month theses round-trip. |
| Q-R04 | **Resolution rule:** `return = price(t0 + horizon) / price(t0) - 1`. No threshold; conviction only affects position size. |
| Q-R05 | **"Paper portfolio" = independent virtual positions**, one per claim, opened at `t0` and closed at `t_resolve`. No shared portfolio or allocation limits at v0. |
| Q-001 | **Structured ingest.** Claims are captured as canonical fields (asset, direction, horizon, conviction, optional thesis). Later extended by Q-028. |
| Q-002 | **Auto-resolvable claims only at v0** (crypto prices). News and narrative claims are deferred. |
| Q-003 | **Crypto-only asset universe at v0.** Equities revisit once a price feed is chosen. |
| Q-004 | **Unified data model:** Source / Claim / Asset / Event / Outcome / Entity / Hypothesis / AttributionLedger. |
| Q-005 | **Immutability via append-only tables**, `claim_edits` JSONB, `override_log` and soft deletes. No cryptographic ingest hash at v0. |
| Q-006 | **Source reputation is Admiralty 2-axis** (`reliability` A–F × `credibility` 1–6), with a `reputation_meta` JSONB column for richer models later. |
| Q-007 | **No hypothesis clustering at v0.** Each claim gets exactly one Hypothesis row and one `originator` attribution row; clustering can arrive later by UPDATE only, with no migration. |
| Q-013 | **Attribution timing anchor deferred**, a direct consequence of Q-007: all v0 attribution rows use `timing_advantage_ms = 0`. |
| Q-023 | **Delisted / split / renamed assets are rejected** with `rejected_asset_mutation`. Those claims neither reward nor penalise the source. |
| Q-024 | **GitHub + GitHub Actions** for hosting and CI. |
| Q-026 | **CoinGecko is the single market-data source** for metadata and resolution. The ±5-minute timestamp snap is accepted; both the requested and returned timestamps are stored so every resolution can be replayed. |
| Q-027 | **Lazy asset resolution.** No seed list and no sync job: `/alpha` searches CoinGecko on demand and `POST /assets/resolve` inserts the row on first pick. |
| Q-028 | **Freeform LLM ingest is the default `/alpha` path.** One sentence goes through a single schema-constrained LLM call (`temperature=0`) into a draft the user confirms or edits. The button form is the fallback when the LLM fails or the monthly budget is used up. About $0.0003 per call. |
| Q-029 | **No `pg_cron`.** Reputation recomputes reactively when each outcome lands; resolution is scheduled with RQ `enqueue_at`. |
| Q-030 | **Durable resolution sweep** (partial reversal of Q-029). Redis eviction silently dropped scheduled jobs, so a worker `CronScheduler` job re-fires any claim more than 6 h past `resolves_at` with no outcome. Postgres is the source of truth; Redis is only a delivery mechanism. ADR (v). |
| Q-031 | **First-call nudge.** A daily worker job DMs onboarded users who haven't made a call yet: at most once a week, at most twice ever, never in their first day. The limits are stored in Postgres, not Redis, following the Q-030 lesson. ADR (w). |

## Open when development stopped

| ID | Question |
| --- | --- |
| Q-008 | Conviction-to-size mapping. Shipped with `$1 / $3 / $5 / $10`. |
| Q-009 | How `source_score` aggregates regret across claims (mean, time-decayed, Sharpe-like, Bayesian-shrunk). |
| Q-010 | Which subset of `source_weight` factors to use at v0 (effectively `historical_accuracy` only). |
| Q-011 | Reputation update equation on each outcome. |
| Q-012 | Anti-gaming thresholds. Shipped with a 50/day soft cap, a 30-minute same-asset cooldown, and shrinkage toward the prior. |
| Q-014 | Cohort policy for launch (size, invites, identity). |
| Q-016 | Entry logic beyond market entry at message time. |
| Q-017 | Exit logic beyond time-based horizons. |
| Q-018 | Twitter/X as a second claim source. |
| Q-020 | Adopting the ML-readiness schema reservations (embedding columns, market-state snapshots at `t0`). |
