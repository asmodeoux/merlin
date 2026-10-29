# MVP Spec — Merlin v0

> **Document role.** The product and data spec for Merlin v0. Covers the canonical schema, full Postgres DDL (§13), operational behaviour (§12), implicit invariants (§15), and an ML-readiness review (§11). Code comments cite this document by section (e.g. `MVP §15 invariant 9`).
>
> **Status.** Written as the build spec for a 30-day MVP. Most of it shipped; §5 and §10 list what was still open when development stopped. Decisions referenced as `Q-NNN` are summarised in [`docs/DECISIONS.md`](./DECISIONS.md).

---

## 0. BLUF — Read this first

**Merlin turns a structured alpha call into a tracked, ranked, regret-scored signal.** A user submits a trade thesis via Telegram in under a minute. The system virtually opens the position at the moment of the message, virtually closes it at the user's chosen horizon, and ranks the user against everyone else by how much money the call *would have* made or lost. The leaderboard is the user-facing answer; everything else is plumbing to make it honest.

### Example — one claim, end to end

A user runs `/alpha` in the Merlin TG bot. The bot walks them through guided questions (buttons; optional free-text thesis with a preview-and-edit step). Their submission becomes:

```
Source:     @yurialpha
Asset:      TON
Direction:  Long
Horizon:    90d
Conviction: Very strong
Thesis:     "Gotta buy 5000 TON, target $5 by August"     (optional, stored, not parsed for scoring)
Submitted:  2026-05-14T13:47:00Z                (append-only audit log; cryptographic anchoring lands at the DLT layer in Phase 2+)
```

The bot persists this as a `Claim`, derives a verifiable `Event` (`return = price(t0 + 90d) / price(t0) - 1`), schedules resolution at `2026-08-12T13:47:00Z`, and *virtually* opens a paper position. No real money — see §2.7.

At resolution, the worker fetches the two TON prices from CoinGecko (with the exact price-point references stored for replay), and writes both counterfactual branches of the regret:

- `pnl_if_followed = direction × observed_return × size(conviction)` — the source's claim acted on
- `pnl_if_ignored = 0` — flat baseline (see §2.7)
- `regret = pnl_if_followed - pnl_if_ignored`

If TON returned **+25%**, the user's regret is **+$2.50** at conviction-Very-strong sizing — the system *should* have followed them. Leaderboard rank up. If TON returned **–10%**, regret is **–$1.00** — they cost the system money. Leaderboard rank down. **Every claim is scored, whether or not the system actually committed capital** — the counterfactual is computed from market data alone.

### What the user sees — the leaderboard

The product surface is three commands:

- **`/board`** — top-N sources by aggregate regret, with Bayesian shrinkage so a single 100× outlier can't dominate. Cold-start sources (N_resolved < 10) hidden from public view until they've earned a real sample.
- **`/me`** — your rank, your open claims, your last-30d PnL, your best and worst signal types, your regret distribution.
- **`/claim <id>`** — full lifecycle of any single claim: who said what when, what the Event was, what the Outcome was, which exact CoinGecko price points resolved it.

The leaderboard is the **direct user feedback loop**. Implicit in it: every claim is paper-traded virtually (the *shadow portfolio*), every regret is tracked, every reputation update is replayable. The math is honest by construction — the same numbers an external auditor would compute given the same source data.

### What's underneath — one paragraph

For each Claim: append-only ingest (audit log + `claim_edits` JSONB; cryptographic / DLT anchoring is Phase 2+) → recorded against a `Hypothesis` (1:1 with Claim at v0; semantic clustering arrives in Phase 2 via UPDATE-only mutations of `claim.hypothesis_id`) → `AttributionLedger` records who originated (passthrough at v0; full origin/amplifier/corroborator semantics in Phase 2 once bucketing exists) → scheduled `Event` resolves to an `Outcome` with both counterfactual branches plus reproducible candle references → reputation update writes a new row in `source_reputation_history` (event-sourced; never overwrites) → future Claims from this Source are weighted by their evolving reputation. **This is the closed loop in Allen's plan**, ported to executable schema (§13 DDL).

> **Math foundation.** The two-sided counterfactual regret in §2.7 is online-optimization regret minimization in the classical sense — for each round (claim), the system picks an action (follow / ignore), the world reveals the outcome, and we score the choice against the best action in hindsight. See MIT 6.7220 L17 — *Online Optimization and Regret Minimization* (Farina, 2024): <https://www.mit.edu/~gfarina/2024/67220s24_L17_online_optimization/L17.pdf>. Allen-recommended reading; the multiplicative-weights update style fits the `source_weight` primitive in Allen's Day-60 design.

---

## 1. MVP Goal (one sentence)

> Ship a **Telegram off-chain bot + DB** that ingests structured trading calls from a closed set of sources, automatically resolves them against market data, and publishes a leaderboard of alpha producers — proving the closed loop end to end.

This is the **Day 30 deliverable** from `ALLEN_PLAN_DRAFT.md`. Everything outside Day 30 (reflexive graph, prediction markets, staking, tokens) is **out of scope** for this document.

---

## 2. Core design

The product rules everything else is built around.

### 2.1 Thesis

Information quality improves when attribution exists, incentives exist, downstream market impact feeds back upstream, and sources develop track records tied to outcomes. The MVP must already close this loop **mathematically**, even if rewards are subsidized and reputation bounds are clamped.

### 2.2 Closed-loop pipeline

```
Message → structured claim → linked hypothesis → confidence score → attribution ledger
       → market move → attribution update → source credibility adjustment → future weighting
```

### 2.3 Telegram bot is structured, not free-form

The bot walks the user through **buttons and guided questions** (asset, direction, horizon, conviction, optional thesis). No free-text NLP at ingest. The bot **is** the human-source adapter that produces canonical `Claim` records (per the data-architecture rationale in `YURI_NOTES.md` §6).

### 2.4 Off-chain, paper-portfolio only

- TG bot + DB.
- **Paper portfolio = virtual / counterfactual** — for every claim, the system simulates a purchase at `t0` and a close at `t_resolve` and compares the two scenarios in §2.7. There is **no single shared portfolio** with allocation constraints at v0; each claim is its own independent virtual position.
- Resolution uses market-data APIs (see §6.2).
- No on-chain components, no token, no real custody.

> The portfolio-level constraints from `JEFF_NOTES.md` (max position $15, max open $60, etc.) describe a Day 60+ "follow policy" agent that selects which claims to act on — they are **not** applied to v0's per-claim regret accounting.

### 2.5 Verification scope — auto-resolvable only at v0

- 🟢 **Auto-resolvable** only — crypto prices, on-chain metrics. (~80% coverage in the advice from `YURI_NOTES.md` §3.)
- 🟡 Semi-manual (news, corporate events) — **deferred** to Day 60+.
- 🔴 Narrative claims ("market bullish") — **excluded**.

Asset universe at v0 is **crypto-only** (top-N liquid majors and alts from CoinGecko's market-cap ranking) — DECISIONS Q-003 deferred 2026-05-25; equity adapter revisits at Day 45+ when a feed is chosen. Binance was dropped 2026-05-26 (Q-026); CoinGecko is the single market-data source for both metadata and resolution.

### 2.6 Leaderboard sanity check

After 30 days the leaderboard must rank a known professional trader above a non-trader baseline account. If it doesn't, the scoring methodology is broken and goes back for revision before launch.

### 2.7 Regret — two-sided counterfactual

For every claim the system computes **two scenarios**, regardless of what was actually done:

1. **`pnl_if_followed`** — what would have happened if the claim were acted on (paper position taken at `t0` in the stated direction, sized per the conviction rule, closed at `t_resolve`).
2. **`pnl_if_ignored`** — what would have happened if the claim were not acted on (no position; baseline is flat, i.e. `0` by default).

**Regret is the delta between the two scenarios.** Sign convention:

```
regret = pnl_if_followed - pnl_if_ignored
```

Interpretation:

- `regret > 0` and source's claim was **ignored** → opportunity cost; the source produced alpha we didn't capture.
- `regret < 0` and source's claim was **followed** → realized loss vs. having stayed flat; the source produced anti-alpha.

The key property: every claim generates a regret signal whether or not a paper position was ever taken, so **every source is scorable from day one** without requiring the system to commit capital per claim. Market data resolves both branches automatically at `t_resolve`.

> **Math foundation.** This is online-optimization regret minimization in the textbook sense. At each round (claim), the learner picks an action (follow / ignore), the world reveals the outcome, regret accumulates against the best action in hindsight. The multiplicative-weights / Hedge-style update is the canonical fit for the Day-60 `source_weight` primitive. Reference: **MIT 6.7220 L17 — *Online Optimization and Regret Minimization* (Farina, 2024):** <https://www.mit.edu/~gfarina/2024/67220s24_L17_online_optimization/L17.pdf>.

#### Implications

- A source's leaderboard score is some aggregation of regret across their claims (mean, sum, time-decayed).
- The "did we follow it?" branch is a separate policy that can vary (always-follow, follow-by-reputation, follow-with-threshold). The regret computation is policy-independent.
- "Ignored" defaults to flat (`0`). An alternative baseline (e.g., hold-the-asset, hold-the-index) is a future option — keep it flat at v0 for simplicity; revisit at Day 60.

> Aggregation rule across a source's claims (mean / sum / time-decayed mean / Sharpe-like). Conviction-to-size mapping that goes into `pnl_if_followed`. Both still needed; the *definition* of regret itself is now locked.

---

## 3. Data model (canonical schema)

> The model combines the nodes needed for attribution (Source, Claim, Hypothesis, AttributionLedger) with the nodes needed to make the loop resolvable (Asset, Event, Outcome, Entity). See Q-004.
>
> **Note:** `Event`, `Hypothesis`, and `Entity` are intentionally **compound objects** — they aggregate multiple sub-fields and derived statistics, not just identifiers.
> - **`Event`** carries `resolution_rule`, `resolution_source`, `t0`, `t_resolve`, and the `asset_state_at_t0` snapshot (price, volume, realized vol, BTC return, regime tag — see §11.1.B). At resolution, candle refs (§11.1.H) join the record so any auditor can replay it.
> - **`Hypothesis`** holds a semantic centroid (vector embedding, populated at Phase 2), `belief_score`, `market_impact`, and `attribution_weights` (JSONB map of source → weight). **At v0, every Claim creates exactly one Hypothesis row (1:1)** — no bucketing or clustering (DECISIONS Q-007). Phase 2 introduces real clustering by mutating `claim.hypothesis_id` UPDATE-only (no schema migration).
> - **`Entity`** is the real-world layer (company / protocol / project) — one entity owns many `Asset`s (equity ticker + options + bonds, or token + governance contract + L2 representation). Deferred to Day 45; the relation `Asset.entity_id` exists at v0 so the column doesn't need a later migration.

### 3.1 Core node types

| Node | Purpose |
| --- | --- |
| **Source** | Who is speaking. Human, account, or system. Holds evolving reliability / specialization / latency / market-impact stats. |
| **Claim** | What is said. Central object. A forecast or assertion. Belongs to a Source, refers to an Asset, predicts an Event. |
| **Hypothesis** | Cluster of related Claims. **v0 stores one Hypothesis per Claim (1:1)** — Q-007. Real clustering arrives at Phase 2. |
| **AttributionLedger** | Records who originated / amplified / corroborated each Hypothesis, with timing advantage. |
| **Asset** | What it is about. BTC, ETH, AAPL, etc. — any tradeable instrument. |
| **Event** | The verifiable proposition the Claim predicts. *Formal*, not narrative. |
| **Outcome** | What actually happened. Resolved Event. |
| **Entity** | Real-world layer — company / protocol / project that issues one or more Assets. |

### 3.2 Minimum relations

```
Source       → makes        → Claim
Claim        → refers_to    → Asset
Claim        → predicts     → Event
Event        → has          → Outcome
Asset        → belongs_to   → Entity
Claim        → contributes_to → Hypothesis
Source       → recorded_in  → AttributionLedger
Hypothesis   → has          → belief_score, market_impact, attribution_weights
```

### 3.3 Concrete record shapes

Reference JSON shapes for each record.

```json
// Source
{
  "source_id": "tg:123456789",
  "handle": "@yurialpha",
  "kind": "human",
  "reputation": {
    "reliability": "F",                   // Admiralty 2-axis. A..F; F = "Reliability cannot be judged" (cold start).
    "credibility": 6,                     // 1..6; 6 = "Truth cannot be judged" (cold start).
    "n_resolved_claims": 0,
    "latency_p50_ms": null,
    "market_impact": 0.0,
    "reputation_meta": {}                 // JSONB, reserved for full Admiralty / AJP-2.1 semantics in Phase 2 (per-asset-class vectors, etc.)
  },
  "created_at": "2026-05-12T10:00:00Z"
}
```

> **Cold-start convention.** Per Admiralty / AJP-2.1, `reliability='F'` and `credibility=6` mean "cannot be judged" — the appropriate state for a brand-new source. As resolved Outcomes accumulate, the reputation update (Q-011) advances both axes. The mapping from accumulated regret to (reliability, credibility) is the open Q-011 + Q-010 work.

```json
// Claim
{
  "claim_id": "clm_01HXY...",
  "source_id": "tg:123456789",
  "asset_id": "asset:crypto:BTC",
  "direction": "long | short",
  "horizon": "1h | 4h | 24h | 3d | 7d | 14d | 30d | 90d",
  "conviction": "weak | medium | strong | very_strong",
  "thesis_text": "optional free-text rationale, not parsed for scoring at v0",
  "submitted_at": "2026-05-12T10:00:01.234Z",
  "event_id": "evt_01HXY...",
  "hypothesis_id": "hyp_01HXY...",     // 1:1 with claim_id at v0; Phase 2 may UPDATE to point at a clustered hypothesis
  "edits": []                          // append-only edit log; never overwrite. Cryptographic anchoring deferred to Phase 2 (DLT layer).
}
```

```json
// Event (derived deterministically from the Claim by the bot at ingest)
{
  "event_id": "evt_01HXY...",
  "asset_id": "asset:crypto:BTC",
  "resolution_type": "auto",
  "resolution_source": "coingecko:bitcoin:usd",
  "resolution_rule": "return = price(t0 + horizon) / price(t0) - 1",
  "t0": "2026-05-12T10:00:01.234Z",
  "t_resolve": "2026-05-13T10:00:01.234Z",
  "status": "pending | resolved"
}
```

> The Event records a **continuous return** over the horizon. There is no binary win/loss threshold — PnL is computed from the return directly (`direction × return × size`). See §2.7 and §5.1.

```json
// Outcome — continuous, both counterfactual branches always populated (see §2.7)
{
  "outcome_id": "out_01HXY...",
  "event_id": "evt_01HXY...",
  "resolved_at": "2026-05-13T10:00:02.001Z",

  "observed_return": 0.0234,           // e.g. +2.34% over horizon
  "price_at_t0": 67421.10,             // pinned at Event creation for reproducibility
  "price_at_t_resolve": 69001.42,

  "pnl_if_followed_usd": 2.34,         // = direction × observed_return × size(conviction)
  "pnl_if_ignored_usd": 0.0,           // flat baseline at v0
  "regret_usd": 2.34,                  // = pnl_if_followed - pnl_if_ignored

  "followed_in_practice": false        // policy flag; does not affect regret computation
}
```

> No `result: "win | loss | flat"` field — sign of `pnl_if_followed` carries that information without ambiguity. PnL stays continuous end-to-end.

```json
// AttributionLedger entry (one per Claim → Hypothesis edge)
{
  "entry_id": "att_01HXY...",
  "hypothesis_id": "hyp_01HXY...",
  "source_id": "tg:123456789",
  "role": "originator | amplifier | corroborator",
  "timing_advantage_ms": 0,
  "downstream_pnl_attributed_usd": 0.0
}
```

### 3.4 Immutability rules

**Principle:** the AttributionLedger only works if claim records cannot be silently rewritten after the market has moved. Hindsight edits and stealth deletions must be impossible.

**Mechanism:**

- Claim ingest is **append-only** in the DB. Edits go into the `claims.edits[]` JSONB array with timestamps; the original payload is never overwritten.
- All admin / scoring mutations land in `override_log` (§12.5) — also append-only.
- Deleted Telegram messages do not delete Claims; the Claim row stays, with a `soft_deleted_at` flag if the source revokes consent.
- **No cryptographic `ingest_hash` at v0.** Cryptographic / DLT-backed tamper evidence ships at the **DLT layer in Phase 2+**  — the schema and audit log are sufficient for honest internal accounting at v0, and the DLT layer will write proofs over the existing audit trail without a backfill.

> Why the DLT-deferred path is honest at v0: a plain append-only audit log + `override_log` + `soft_deleted_at` already makes hindsight edits and stealth deletions detectable to anyone with read access to the DB. The cryptographic anchor adds *external* verifiability — useful when third parties enter the loop in Phase 2; not strictly required for internal correctness now.

---

## 4. Telegram bot flow

> structured input; exact wording and screen sequence.

### 4.1 Onboarding (one-time per source)

1. `/start` → prompt for display name + accept ToS.
2. Optional: link wallet address (recorded, not required for v0).
3. Source record created with zeroed reputation.

### 4.2 Claim submission flow

Triggered by `/alpha` or by any non-command message (the bot treats freeform text as the start of a claim — Q-028).

**Freeform-first (default path).**

1. **User types.** "ETH long 14d strong, Vitalik rumour" — anything that looks like a market call.
2. **Bot calls `POST /claims/draft`.** One LLM call (OpenRouter, `gpt-5-mini` primary with `gemini-3.5-flash` fallback, `temperature=0`, JSON-schema constrained, reasoning effort `minimal`). Stateless — no DB writes.
3. **LLM returns a structured draft** — direction, horizon (quantised to the supported buckets), conviction, thesis text, plus extracted intent in `extracted_fields` (target price, calendar date hint, named entities, rumour flag). Per-field confidences accompany every value.
4. **If the asset query is confident**, the API proxies CoinGecko `/search` and returns up to 5 candidates inside the draft response.
5. **Bot renders the preview.** Single message with the prefilled fields and an inline keyboard: `Confirm` · `Edit asset` · `Edit direction` · `Edit horizon` · `Edit conviction` · `Edit thesis` · `Cancel`. The user's verbatim thesis is quoted on the preview. **Nothing else from `extracted_fields` is shown** — the "silent capture" rule (see §15 invariant 17 and the bot-voice rules in `PLAN.md`).
6. **Edit jumps inline** to the matching button step and back to the preview with the field overwritten. Confirm composes `SubmitClaimRequest` and calls `POST /claims`.

**Button fallback.**

If `POST /claims/draft` 5xxs, OpenRouter is rate-limited, or `LLM_MONTHLY_USD_BUDGET` is exhausted, the bot drops into the deterministic button flow starting at the asset step. The user sees no "fallback" message — the bot simply asks "Which asset?" as if that were the intended first step. **No fallback paths in any downstream component** — the resolver never knows whether the claim was parsed by LLM or buttons (it sees `claims.parsed_via` if it cares).

The button-flow steps remain available and unchanged:

| Step | Question | Input type |
| --- | --- | --- |
| 1 | Which asset? | Free-text query → CoinGecko `/search` proxied via the API → inline keyboard of top 5 candidates → on pick, API materialises the `assets` row (INSERT-or-fetch by `coingecko_id`). Returns `asset_id`. **Lazy resolution; no pre-seeded catalog** — Q-027. See §14. |
| 2 | Which way? | Buttons: `Long` / `Short`. |
| 3 | How long until you find out? | Buttons: `24 hours` / `3 days` / `1 week` / `2 weeks` / `1 month` / `3 months`. *(Internal horizon codes are `24h` / `3d` / `7d` / `14d` / `30d` / `90d` — the user never sees the codes.)* |
| 4 | How sure are you? | Buttons: `Weak` / `Medium` / `Strong` / `Very strong`. |
| 5 | In your own words — what's the call? | Free text. Stored verbatim in `claims.thesis_text`. |
| 6 | Preview + confirm | Same preview as the freeform path. Edit any field; Confirm persists the Claim. |

> **UX intent.** The freeform path is faster and captures nuance ("$2000 target", "by earnings", "Vitalik rumour") that the button flow can't ask for without ballooning the conversation. We **persist** that nuance into `claims.extracted_fields` for the record; the v0 verifier doesn't score it, so we don't surface it on the preview. The button flow remains the source of truth for the field schema — every freeform draft is editable via the same buttons. Once Confirmed, the Claim is append-only forever (§3.4 / §15).

On confirm:

- Claim is persisted (append-only; audit log via `claims.edits` and `override_log`, see §3.4).
- Event is generated deterministically from `(asset, direction, horizon, conviction)`.
- Hypothesis assignment: **1:1 with Claim at v0** (Q-007). One Hypothesis row per Claim; bucketing arrives at Phase 2 via UPDATE-only mutations of `claim.hypothesis_id`.
- AttributionLedger entry created with `role='originator'`, `timing_advantage_ms=0`, `timing_advantage_anchor='first_claim_in_hypothesis'`. v0 is a passthrough; full origin/amplifier/corroborator semantics arrive at Phase 2 (Q-013 chained-resolved 2026-05-25).
- Job scheduled at `t_resolve` to query the price API and write the Outcome.

### 4.3 User-facing read commands

- `/me` — your reputation, open claims, last 30d simulated PnL, regret, rank.
- `/board` — top-N leaderboard (current and 30d rolling).
- `/claim <id>` — inspect a specific claim's status, event rule, and outcome (if resolved).

### 4.4 Anti-gaming, v0 minimum

- **Per-source daily soft cap** on claims (**50/day** — legitimate alpha producers can exceed 10 in active markets; the actual constraint we want is "no asset spamming").
- **Same-asset cooldown**: no two claims by the same source on the same asset within **30 minutes**. Catches volume farming on a single ticker without penalizing high-throughput legitimate sources.
- **Min-sample shrinkage**: rank score uses Bayesian shrinkage to a population prior until `N_resolved ≥ 10`.
- **Append-only audit log** (see §3.4) — no hindsight edits possible.
- **No microcap loophole**: at v0 restrict the asset universe (see §2.5 / §6.1).
- Conviction-weighted sizing is bounded; one "100x" win cannot dominate the leaderboard.
- **Low-quality-source demotion is the reputation update's job, not a parallel mechanism** — keeps scoring logic in one place and avoids coupling Q-011 with Q-012.

---

## 5. Scoring, regret, and reputation update

The formulas behind the leaderboard. Rows marked open were still being tuned when development stopped; the shipped code uses the proposed values.

### 5.1 Required formulas

| Formula | Status |
| --- | --- |
| `regret(claim, outcome) = pnl_if_followed - pnl_if_ignored` | — see §2.7. |
| `pnl_if_followed = direction × observed_return × size(conviction)` | — proposed: `weak=$1 / medium=$3 / strong=$5 / very_strong=$10`. Confirm or amend in `DECISIONS.md` Q-008. |
| `pnl_if_ignored = 0` (flat baseline) | — see §2.7. |
| `source_score(source)` — aggregation of regret across the source's resolved claims (mean / sum / time-decayed / Sharpe-like) | Open — Q-009. |
| `source_weight(source) = f(historical_accuracy, market_timeliness, originality, downstream_influence)` — Day 60 primitive; v0 may use a simplified subset (e.g., historical_accuracy only). | — note `market_timeliness`, `originality`, `downstream_influence` all depend on Phase-2 hypothesis bucketing (Q-007 deferred), so v0 effectively reduces to `historical_accuracy` only. |
| `belief_score(hypothesis)` update on Outcome | **[DEFERRED to Phase 2 — DECISIONS Q-011]** Trivial at v0 (1 Hypothesis = 1 Claim per Q-007); meaningful update equation lands with bucketing. |
| `attribution_weights` update on Outcome | **[DEFERRED to Phase 2 — DECISIONS Q-011]** Same dependency on bucketing. |

### 5.2 Bootstrap allowances

Per Day 30 deliverable, the v0 system may use:

- Subsidized rewards
- Clamped reputation bounds (e.g., score ∈ [−1, +1])
- Manual analyst overrides (an `/override` admin command)

But the loop **must already close mathematically** — i.e., once an Outcome lands, the reputation update is automatic and propagates to the next Claim's weighting.

---

## 6. Asset universe and resolution sources

### 6.1 Asset universe v0

Three candidates, pick one:

- **A.** Crypto-only — top N liquid majors and alts. Fastest to ship, fits 24/7 resolution.
- **B.** Crypto + select equities (e.g., the "AWS softening" example) — needs an equity price feed.
- **C.** Crypto + equities + Polymarket events — broadest, slowest. Recommended **against** for v0.

**Chosen: A.** It minimizes integrations. B can be added at Day 45 with a single equity adapter once a feed is chosen.

### 6.2 Market-data sources

- Crypto prices: **CoinGecko Demo (free tier)** is the single source for both asset metadata and resolution price points. Granularity is auto-determined by CoinGecko based on requested range: 5-minute buckets for ≤24h horizons, hourly for 2–90d, daily for >90d (Q-026). The resolver records the **actual CoinGecko-returned timestamp** alongside the requested timestamp so audits replay exactly.
- The adapter wraps three endpoints: `/search` (drives the bot's `/alpha` asset step — Q-027 lazy resolution, §14), `/coins/{id}/market_chart/range` (resolution price points), `/simple/price` (live spot for board displays).
- On-chain (if used at v0): a single chain to start — Ethereum mainnet via Alchemy or similar.
- Equities (if §6.1 = B): Polygon.io or Alpha Vantage.

> All resolution sources are recorded per-Event in `resolution_source` so methodology is auditable.

---

## 7. Source onboarding

1. **Cohort size.** Start with ~10 invited users; ramp once the Day-30 sanity check passes.
2. **Invite policy.** Invite-only. Every source joins through an invite code, so each account has a traceable referrer.
3. **Identity.** Pseudonymous is fine; a wallet is recorded if provided. Raw Telegram handles are never logged (§15 invariant 9).

Referral-code plumbing lands in Sprint 1; sources can self-issue invite codes from Sprint 4 onward. That is the hook for controlled growth: the invite chain doubles as sybil resistance. Public open-up is deferred until the leaderboard sanity check passes.

> **Why referral codes from Sprint 1 even though they're a Sprint-4 user surface:** the schema and table writes are cheap (`invites.created_by_source_id` already exists in §13). Lighting them up early avoids a migration when Phase 2 introduces self-issued invites and downstream-influence tracking via referral chains.

---

## 8. Implementation proposal

How the product is built. Nothing in this section changes product behaviour.

### 8.1 Stack

| Concern | Choice | Rationale |
| --- | --- | --- |
| Language | Python 3.12 | Fastest path for trading-data work; aligns with eventual ML. |
| Bot framework | `python-telegram-bot` v21+ | Mature, well-documented, async. Webhook + long-polling both supported. |
| DB | Postgres 16 + `pgvector` extension | Single store for relational + JSONB + embeddings (§11). No second store. |
| ORM / DB layer | SQLAlchemy 2.x (async) | Pairs with `python-telegram-bot` async; future-safe for the scoring service. |
| Migrations | Alembic | Standard with SQLAlchemy. |
| Job queue + scheduling | Redis + RQ (incl. native `enqueue_at`) | The **only** scheduling primitive at v0. `POST /claims` calls `queue.enqueue_at(t_resolve, resolve_outcome, claim_id)`; the worker pulls when the wall-clock fires. No table scan ever asks "what's due". |
| Cron / recurring | worker `CronScheduler` (two jobs) | Q-029: reputation recomputes reactively (enqueued by the resolver when an outcome lands); invite expiry checked at redemption; failed-job retries via RQ `Retry(max=3)` + the failed-jobs registry; no `pg_cron`. Q-030 adds the resolution sweep (re-fires bets whose `enqueue_at` job was lost from Redis — eviction / worker OOM / restart). Q-031 adds the first-call nudge (activation DMs to named, never-bet testers; per-user limits in the durable `engagement_nudges` table). |
| Market data | CoinGecko (Demo tier free; same `COINGECKO_API_KEY` env var carries forward to paid tiers — the base URL discriminates) — single source for metadata + resolution | Reproducibility: resolver stores the requested timestamp + the actual CoinGecko-returned timestamp + the granularity bucket. Q-026. See §11.1.H. |
| LLM (freeform ingest only) | OpenRouter (`gpt-5-mini` primary, `gemini-3.5-flash` fallback via OpenRouter's automatic routing) | Drives the freeform claim-draft path (§4.2 / Q-028). Single call per draft. `temperature=0`, `reasoning.effort=minimal`, JSON-schema constrained. Sprint-1 live calibration: ~$0.00028 per call at the primary, ~$0.00125 at the fallback. Budget circuit-breaker: `LLM_MONTHLY_USD_BUDGET` short-circuits to the button form before exceeding cap. |
| Hosting | **Fly.io** + Fly Postgres add-on | Zero-ops, `fly deploy` from git, managed TLS for webhooks, daily snapshots included. Vultr was the discussed alternative but discarded — ops overhead is the enemy of a 30-day sprint. |
| Secrets | Fly.io secrets in prod, `.env` locally | No vault for v0. |
| Logging | `structlog` → JSON to stdout → Fly.io log shipping | Trivially parseable for later analysis; no extra log infra. |
| Errors / observability | Sentry + Fly metrics | Minimum to debug. Add Grafana later if traffic warrants. |
| Linting / formatting | `ruff` + `ruff format` | Replaces black + isort + most of flake8 in one tool. |
| Type checking | `mypy --strict` on `/score` and `/resolve`; default elsewhere | Strict where math/correctness lives; relaxed in bot glue. |
| Test runner | `pytest` + `pytest-asyncio` | Default. |
| Pre-commit | `ruff` + `mypy` + `pytest -m fast` | Catch obvious breakage before push. |
| VCS hosting | **GitHub**. | Familiar, free for our team size, no self-hosting overhead. Self-hosted Gitea was the prior working hypothesis; dropped 2026-05-25. |
| CI | **GitHub Actions** | Free minutes on private repos for our team size; no separate runner ops. |
| Backups | Fly Postgres daily snapshots (built-in) | Sufficient at v0. External off-Fly backups (e.g., weekly `pg_dump` to S3) deferred to Phase 2 review, when the dataset becomes high-value. |
| Frontend hosting | React (Next.js or Vite) served by **Fly** as a separate app | Single platform with the backend — one deploy pipeline, one invoice, no CORS gymnastics. No third-party frontend host (Vercel / Netlify / Cloudflare Pages) at v0. |
| Bot timezone display | UTC, with `(UTC)` suffix on every confirmation | Avoids DST / locale bugs. Storage is already UTC throughout. |

**Stack explicitly NOT proposed for v0:**

- Graph DB — Postgres + a `relations` table covers v0; revisit at Day 60 when reflexive belief graph requires propagation.
- React frontend — replaced by the TG bot per §2.3.
- ML / Bayesian scoring service — defer until the scoring formulas are locked and ≥1000 resolved Claims exist.

### 8.2 Repo layout

**Canonical layout lives in [`docs/ARCHITECTURE.md`](./ARCHITECTURE.md) §3.** The historical `/bot`, `/ingest`, `/normalize`, `/resolve`, `/score`, `/api`, `/db`, `/admin` top-level layout below has been superseded by the polyglot-monorepo scaffold (`apps/{api,bot,worker}` + `packages/python/{merlin_core,merlin_infrastructure,merlin_client}` + `deploy/`); the responsibilities map as follows for orientation:

| Original §8.2 dir | Lands in                                                                                          |
| ----------------- | ------------------------------------------------------------------------------------------------- |
| `/bot`            | `apps/bot/merlin_bot/`                                                                            |
| `/ingest`         | `apps/bot/merlin_bot/` (Sprint 1) + `merlin_core.application.use_cases` (ingest use cases)        |
| `/normalize`      | `merlin_core.domain` (asset/time/entity normalization is pure)                                    |
| `/resolve`        | `apps/worker/merlin_worker/jobs/` (RQ resolution jobs)                                            |
| `/score`          | `merlin_core.application.use_cases` + `merlin_core.domain` (regret math is pure)                  |
| `/api`            | `apps/api/merlin_api/`                                                                            |
| `/db`             | `packages/python/merlin_infrastructure/merlin_infrastructure/db/` + `packages/python/merlin_infrastructure/alembic/`    |
| `/admin`          | `apps/bot/merlin_bot/admin/` (admin Telegram commands) + admin routes in `apps/api/merlin_api/routes/` |

See `docs/ARCHITECTURE.md` for the why (hexagonal layering, import-linter contracts, multi-frontend strategy).

### 8.3 Build sequence (proposed 30-day sprint)

| Week | Deliverable |
| --- | --- |
| 1 | DB schema + migrations, bot skeleton, `/start` + `/alpha` happy path persisting Claims + Events (no resolution yet). |
| 2 | CoinGecko integration (single source — metadata + resolution prices), scheduled resolution worker, Outcome rows written, `/claim <id>` works. |
| 3 | Scoring service stub with the §5 formulas plugged in; `/me` and `/board` working; daily reputation recomputation. |
| 4 | First cohort onboarded; live for 7 days; sanity check (§2.6) executed; bug-fix + admin tooling. |

> **Hard blocker for Week 3:** §5 formulas must be delivered. The scoring service can be scaffolded earlier with placeholders, but the leaderboard cannot ship without the sizing rule and the reputation update equation.

---

## 9. Out of scope for v0 (explicit)

Deliberately **deferred** to Day 60 / Day 90:

- Reflexive belief graph and propagation.
- Narrative velocity / contagion estimation.
- Prediction market mechanics, confidence staking.
- Anti-sybil weighting beyond invite-only.
- Adversarial detection.
- Token, on-chain attestations, smart contracts.
- An agent-driven "arena" product built on top of this data — that is a **later product surface** that consumes this MVP's data and is not part of the Day 30 build.
- React frontend, public profile pages.
- Cross-ticker company-entity links — additive, can land at Day 45.

---

## 10. Open items when development stopped

| # | Item | Status |
| --- | --- | --- |
| 1 | ~~Asset universe v0~~ | Resolved — crypto-only at v0 (Q-003). |
| 2 | Conviction-to-size mapping for `pnl_if_followed` (Q-008) | Shipped with the proposed `$1/$3/$5/$10`. |
| 3 | `source_score` aggregation rule (Q-009) | Open. |
| 4 | `source_weight` v0 subset (Q-010) | Open. v0 is effectively `historical_accuracy` only. |
| 5 | Reputation update equation on Outcome (Q-011) | Open. |
| 6 | ~~Hypothesis bucketing rule at v0~~ | Resolved — 1:1 with Claim at v0 (Q-007). |
| 7 | Anti-gaming thresholds (Q-012) | Shipped with the §4.4 values. |

> The full list lives in `DECISIONS.md`.

---

## 11. Data-model critique with ML in mind

> Why this section exists: the v0 schema ships the leaderboard, but it will also become the *training set* for everything downstream — source-credibility models, claim-success prediction, hypothesis clustering, collusion detection, reflexive-impact modeling. Several v0 choices are cheap to make ML-friendly **now** and expensive to retrofit later. This section flags those gaps and proposes cheap forward-compatible additions. None of these change v0 behavior; they reserve the slots ML will need.

### 11.1 Top critiques

#### A. Reputation is mutated in place — destroys ML training data

`Source.reputation` (Admiralty 2-axis: `reliability` + `credibility`, plus `reputation_meta` JSONB, Q-006) is mutated in place on every Outcome. For any model that predicts "given the source's state at the moment of a past claim, how would it perform?", we need the reputation **as it was at `submitted_at`**, not as it is today. Mutating in place erases this.

**Cheap v0 fix:** add an event-sourced history table that mirrors the new 2-axis reputation.

```sql
SourceReputationHistory(
  source_id, effective_at,
  reliability CHAR(1), credibility SMALLINT, reputation_meta JSONB,
  n_resolved, specialization JSONB, market_impact, latency_p50_ms, ...
)
```

Insert a new row on every reputation update; never overwrite. The current view is `MAX(effective_at)` per source.

#### B. No market state captured at `t0`

`pnl_if_followed` is the label. Models need features alongside the label: what was BTC doing? What was the asset's 24h volatility? Was the order book thin? None of this is captured.

**Cheap v0 fix:** add a snapshot blob to each `Event` at creation time.

```json
"asset_state_at_t0": {
  "price": 67421.10,
  "volume_24h_usd": 12345678900,
  "realized_vol_24h": 0.034,
  "btc_return_24h": 0.011,
  "regime_tag": "trend_up"     // rule-based: trend_up / chop / drawdown
}
```

One additional API call at ingest. No new infrastructure. Massive value for ML later.

#### C. Independence detection has no schema hooks

`YURI_NOTES.md` §2 explicitly says:

> Edge lives in: (1) correct normalization, (2) entity resolution, (3) **independence detection**.

Without it, ML over-weights popular narratives — a rumor reshared by 10 sources becomes 10 signals instead of 1.

**Cheap v0 fix:** reserve a `claim_similarity_edge` table (empty at v0):

```sql
ClaimSimilarityEdge(
  claim_a_id, claim_b_id,
  cosine_similarity FLOAT,
  detected_at,
  method TEXT   -- 'exact_match', 'embedding', 'manual', ...
)
```

Day 60 fills it. The leaderboard still ships at v0 without it.

#### D. Outcome label is too thin

A single scalar `pnl_if_followed` at a single horizon throws away most of the trajectory. For ML, several extra fields are nearly free at resolution time (already pulling CoinGecko price-series data):

```json
"intermediate_returns": { "1h": 0.004, "4h": 0.011, "24h": 0.018, "7d": 0.022 },
"max_favorable_excursion": 0.041,
"max_adverse_excursion": -0.012,
"benchmark_return": 0.011,           // BTC over same window for crypto alts
"benchmark_relative_return": 0.0124  // observed_return - benchmark_return
```

These let ML train on *trajectory shape* and *relative alpha*, not just terminal PnL. Cheap. Recommended for v0.

#### E. AttributionLedger timing is anchorless

`timing_advantage_ms` is currently a bare number — advantage relative to *what*? The first claim in the hypothesis? The earliest external news? A price move?

**Cheap v0 fix:** add an explicit anchor field.

```json
"timing_advantage_ms": 12345,
"timing_advantage_anchor": "first_claim_in_hypothesis"
// alternatives: 'price_move_threshold', 'external_news', 'manual_anchor'
```

Day 60 can add anchor types without a migration.

#### F. No hook for `thesis_text` embeddings

v0 ignores `thesis_text` for scoring, but that text is the *only* unstructured input. ML will want to embed it later for hypothesis clustering and originality scoring.

**Cheap v0 fix:** reserve a nullable vector column.

```sql
Claim.thesis_embedding vector(384) NULL    -- pgvector
Hypothesis.thesis_centroid vector(384) NULL
```

Populate Day 60+ with an embedding model. No v0 inference cost.

#### G. Source soft-delete only

If a source deletes their account, the schema must not lose their historical claims and outcomes — that would corrupt the training set with survivorship bias.

**Cheap v0 fix:**

```sql
Source.soft_deleted_at TIMESTAMP NULL
```

Hard delete is forbidden. UI hides soft-deleted sources from the leaderboard but keeps the rows.

#### H. Reproducibility of resolution

If we ever need to re-run a historical Outcome (the sizing rule changes and we want to recompute the leaderboard), we need to know exactly which prices were used. `resolution_source = "coingecko:bitcoin:usd"` alone isn't enough — CoinGecko's free tier snaps requested timestamps to its native granularity bucket (5-min ≤24h, hourly 2–90d, daily >90d), so the resolver must store both the **requested** and the **actually-returned** timestamps for replay.

**v0 fix (Q-026):** store both timestamps + the granularity bucket CoinGecko returned.

```json
"resolution_source": "coingecko:bitcoin:usd",
"resolution_price_t0_ref":      "coingecko:bitcoin:usd:5m:1715508060:actual=1715508000",
"resolution_price_resolve_ref": "coingecko:bitcoin:usd:5m:1715594460:actual=1715594400"
```

Ref format: `coingecko:{coin_id}:{vs_currency}:{granularity}:{requested_unix}:actual={actual_unix}` — an auditor replays by calling `GET /coins/{coin_id}/market_chart/range?vs_currency={vs_currency}&from={actual_unix}&to={actual_unix + 1}` and must get the same price back.

**Precision note.** For 1h-horizon claims, the ±5-minute snap on both endpoints means the realized return carries ~5–10 minutes of timing noise relative to the requested timestamps. Accepted at v0 as the cost of running on a free single-source data feed. Day-45+ revisit if leaderboard noise becomes a real problem (Q-008/Q-009 stress test in §2.6 will catch it).

Day-60 audits and formula recomputes become trivial.

#### I. No regime / macro context

Sources who shine in a bull trend may collapse in chop. Without a regime tag the model can't condition on this.

**Cheap v0 fix:** the `regime_tag` field in 11.1.B above. Rule-based at v0 (e.g., "BTC 50d MA slope sign + 30d realized vol bucket"), upgradeable to learned regime detection later.

#### J. PII / anonymization not modeled

TG handles are PII. If we want to share or open-source the training set later, we need an anonymization path planned upfront. Cheap: store `source_id` as opaque (`tg:hash(handle)`) and keep the handle in a separate, access-controlled `SourceIdentity` table.

### 11.2 What the schema already does right (preserved here so it's not lost in future edits)

- **Append-only Claim with `edits[]`** — temporal integrity at the claim level.
- **Millisecond timestamps** — enough precision to order claims within the same minute.
- **Claim / Event / Outcome separated** — clean label/feature boundary for supervised ML.
- **Append-only audit log** (`claims.edits`, `override_log`) — internal tamper evidence; cryptographic / DLT-backed external tamper evidence ships at Phase 2.
- **`thesis_text` preserved** — door open for NLP without re-collection.
- **Raw `pnl_if_followed` + `pnl_if_ignored`** stored separately — `regret` is *derived*, so the sizing formula can change without losing historical labels.

### 11.3 Suggested v0 schema additions (consolidated)

All listed in `DECISIONS.md` Q-020; none change v0 behavior.

| # | Addition | Cost at v0 | ML value |
| --- | --- | --- | --- |
| 1 | `SourceReputationHistory` event-sourced table | 1 INSERT per update | High — historical features |
| 2 | `Event.asset_state_at_t0` JSONB snapshot | 1 extra API call | Very high — context features |
| 3 | `ClaimSimilarityEdge` table (empty at v0) | none | High — independence detection |
| 4 | `Outcome.intermediate_returns`, `MFE`, `MAE`, `benchmark_return` | a few extra candles fetched | High — trajectory + relative alpha |
| 5 | `AttributionLedger.timing_advantage_anchor` enum | none | Medium |
| 6 | `Claim.thesis_embedding`, `Hypothesis.thesis_centroid` (nullable vectors) | pgvector install only | High — Day 60 clustering |
| 7 | `Source.soft_deleted_at` | none | High — survivorship integrity |
| 8 | `Outcome.resolution_price_*_ref` | a few characters | Medium — reproducibility |
| 9 | `regime_tag` in `asset_state_at_t0` | none beyond #2 | High — conditional models |
| 10 | `SourceIdentity` separation for PII | one extra table | High — open-data path |

 Adopt all ten at v0. Total schema impact: ~4 new tables / columns, ~1 extra API call at ingest, ~3 extra CoinGecko price-series fetches at resolution. Effectively free vs. retrofit cost in 6 months.

---

## 12. Operational notes

Concrete behaviors the implementation must honor, beyond the product spec above. Most are pure implementation; the few product calls are flagged.

### 12.1 Resolution worker — retry, backoff, give-up policy

When `t_resolve` fires, the worker fetches `price_at_t0` and `price_at_t_resolve` from the Event's `resolution_source`.

- **Happy path:** both candles fetched → Outcome written exactly once → emit `outcome.resolved` event.
- **Transient failure (HTTP 5xx, timeout, rate-limit):** exponential backoff retry (`30s → 1m → 2m → 5m → 15m → 1h`). Max 6 attempts over ~1.5 h.
- **Persistent failure after retries:** mark Event `status = "resolution_failed"`, log to Sentry, alert. Do **not** write a fake Outcome. The Claim stays in the source's pending list until manually resolved or marked `unresolvable`.
- **Stale jobs (Q-030):** the resolution-reconciliation sweep re-queues any claim whose `resolves_at` is more than 6 h in the past and still has no `outcomes` row. The durable backstop against worker outages **and** RQ jobs lost from Redis (Upstash eviction / worker OOM-SIGKILL / Redis restart) — the failure mode that silently dropped bets before Q-030. Implemented as an RQ `CronScheduler` job (`apps/worker/merlin_worker/schedules/cron.py`); manual one-shots via `make admin-rerun CLAIM_ID=…` / `make admin-backfill`.

### 12.2 Resolution edge cases

| Case | v0 behavior |
| --- | --- |
| Exchange / API outage at `t_resolve` | Retry policy §12.1. After give-up, manual operator intervention. |
| Asset delisted / split / renamed between `t0` and `t_resolve` | **Reject the mutation outcome.** Set `events.status = 'rejected_asset_mutation'`. **No `Outcome` row is written.** Affected claims contribute neither rewards nor penalties to the source's `source_score` aggregate (the leaderboard query filters on `events.status = 'resolved'`). |
| Detection of mutation | Piggybacks on the CoinGecko error path — when the resolver's price-range fetch returns a 404 / empty-series error for the asset's `coingecko_id` *and* `asset_aliases` has no successor mapping, the resolver flips status to `'rejected_asset_mutation'` rather than `'resolution_failed'`. No separate crawler. |
| Operator-mapped rename (e.g., `XYZ → XYZ2` with continuity) | If an operator maps the new symbol in `asset_aliases` (§14) **before** `t_resolve`, the resolver follows the alias chain and writes a normal Outcome. If the alias is added after rejection, the rejection stands; operator may issue a one-off `/override` to recompute. |
| Missing candle at the exact second | Use the next available candle within ±5 minutes. If none, treat as transient failure (§12.1). |
| Stablecoin depeg (claim was "long USDC") | No special handling. Return is computed normally; if USDC depegs, the source eats it. |
| Time-zone or DST | All storage and computation in UTC. No DST math anywhere. |

### 12.3 Concurrency — tie-breaking for `originator` role

> At v0 every Hypothesis contains exactly one Claim (Q-007 RESOLVED 2026-05-25), so the originator is unambiguous and the tie-breaker below is dead code. **Reserved for Phase 2** when bucketing introduces multi-claim hypotheses. The rule is captured here so the implementation is ready when bucketing flips on.

When two Claims contribute to the same Hypothesis within the same millisecond:

```
originator = sort_by(submitted_at_ms ASC, source_id ASC).first()
```

Deterministic, replayable, no manual input needed.

### 12.4 Cold-start UX for new sources

Until a source has `N_resolved >= 10` (matches Q-012's default shrinkage threshold):

- Their `source_score` is computed using Bayesian shrinkage to the population mean.
- They are **not shown** on the public `/board` leaderboard.
- Their own `/me` command shows the unshrunk score with a "needs N more resolved claims" label.
- They can submit claims normally; the system just doesn't rank them publicly yet.

This prevents a single 100× claim from immediately topping the board and prevents cold-noise from dominating the surface area.

> This is a UX choice; if we want public ranks from claim 1, this is a one-flag change.

### 12.5 Admin `/override` is itself immutable

Per §5.2, the v0 allows manual analyst overrides. Implementation:

```
OverrideLog(
  override_id, operator_id, target_table, target_id,
  field, old_value, new_value, reason, created_at
)
```

- Append-only. Never updated. Never deleted.
- Every UI/admin command writes a row.
- `/audit <claim_id>` shows the full override history for any record.

Auditing the auditor is what keeps the closed loop honest under bootstrap.

### 12.6 Source invite mechanics + referral codes

Per Q-014, v0 is invite-only. Implementation lays the **referral-code plumbing** in Sprint 1 (controlled growth + gamification); the user-facing surface goes live in Sprint 4.

**Sprint 1 (plumbing — admin-only):**

- Admin command `/invite <handle>` issues a one-time code (`inv_01HXY...`).
- Code is delivered out-of-band (DM, Signal, email — admin's choice).
- New user opens the bot, runs `/redeem <code>`, completes onboarding flow §4.1.
- Codes have a `created_by_source_id`, `redeemed_by_source_id`, `expires_at` (default 14 days).
- Unredeemed codes are revocable by the issuer.
- The `created_by_source_id` column **is** the referral attribution edge — no separate `referrals` table needed at v0.

**Sprint 4 (user surface):**

- Each source can self-issue ≤ N personal codes via `/myinvites` (N tunable, default 3).
- `/me` shows total redeemed count for the source — first piece of the gamification surface.
- Phase 2 reads `created_by_source_id → redeemed_by_source_id → outcomes` chains as the **`downstream_influence`** input to the Day-60 `source_weight` primitive (Q-010 option C).

**Waitlist (parallel surface, Sprint 1 plumbing):**

Anyone who arrives at the bot without a code is captured in a `waitlist` table for ops triage. Single source of truth for "people who tried to join" so growth is observable from day 1.

```
waitlist(
  waitlist_id, tg_user_id, tg_handle, requested_at,
  status TEXT,                          -- 'pending' | 'invited' | 'rejected' | 'expired'
  invite_id TEXT NULL,                  -- set when status flips to 'invited'
  notes TEXT NULL                       -- admin-only
)
```

`Invites` table (unchanged from prior draft):

```
invites(
  invite_id, code, created_by_source_id, created_at,
  expires_at, redeemed_by_source_id NULL, redeemed_at NULL,
  revoked_at NULL, revoked_reason NULL
)
```

### 12.7 Monitoring / SLA stub

| Signal | Target | Alert |
| --- | --- | --- |
| Bot webhook 5xx rate | < 0.5% per 5 min | Sentry alert |
| Resolution worker lag (oldest pending Event past `t_resolve`) | < 30 min | Sentry alert |
| Daily reputation recompute heartbeat | Runs by 00:15 UTC | Missing heartbeat → Sentry alert |
| DB connection pool exhaustion | Never | Hard alert |
| Disk free on Fly Postgres | > 30% | Slack alert |

Concrete dashboards out of scope at v0; alerts are sufficient.

### 12.8 Backup / DR

- **Fly Postgres daily snapshots** (built-in, retained per Fly's policy) are the only backup mechanism at v0.
- External off-Fly backups (e.g., weekly `pg_dump --format=custom` to an S3-compatible bucket) are **deferred**. Revisit at the Phase 2 review when the dataset becomes high-value (≥ 1000 resolved claims and the first ML training-set export landing).
- Restore drill: once before launch, document the steps in `docs/RUNBOOK.md`.
- DR posture under "Fly account is gone" scenario is acknowledged as a known gap until external backups land.

### 12.9 Resolution candle reference format

Per §11.1.H, every Outcome stores the exact candles used:

```
price_ref = "{source}:{coin_id}:{vs_currency}:{granularity}:{requested_unix}:actual={actual_unix}"
example:   "coingecko:bitcoin:usd:5m:1715508060:actual=1715508000"

# granularity ∈ {"5m", "1h", "1d"} — auto-determined by CoinGecko Demo
# tier from the requested range (≤24h → 5m, 2–90d → 1h, >90d → 1d).
# `actual_unix` is the timestamp of the price point CoinGecko actually
# returned (≠ requested when CoinGecko snapped to a bucket boundary).
# Q-026.
```

This is the smallest possible artifact that lets any auditor replay the resolution years later.

### 12.10 Bot display formatting rules

- Numbers: 2 decimal places for USD, 4 decimal places for returns, scientific notation forbidden.
- Timestamps: ISO 8601 UTC, e.g., `2026-05-13 10:00 UTC`.
- Asset names: canonical symbol only (`BTC`, not `bitcoin`).
- All confirmation messages end with the exact `t_resolve` and the canonical `claim_id`.

---

## 13. Concrete DB DDL

PostgreSQL 16 + `pgvector`. Naming convention: snake_case tables and columns, singular field names, `_id` for foreign keys, `_at` for timestamps (always `TIMESTAMPTZ`), `_usd` suffix for monetary values.

```sql
-- ============================================================
-- Extensions
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector
-- Q-029: no pg_cron. All scheduling
-- lives in Redis (RQ Scheduler `enqueue_at`). Reputation recomputes
-- reactively on outcome write. See §10 stack table and §15 below.

-- ============================================================
-- Identity & sources
-- ============================================================

CREATE TABLE sources (
  source_id            TEXT PRIMARY KEY,            -- e.g. 'tg:hash(handle)'
  kind                 TEXT NOT NULL,               -- 'human' | 'agent' | 'system'
  display_name         TEXT NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  soft_deleted_at      TIMESTAMPTZ NULL,
  consent_version      TEXT NOT NULL,
  wallet_address       TEXT NULL,                   -- optional, opaque
  -- Admiralty 2-axis reputation
  reliability          CHAR(1) NOT NULL DEFAULT 'F'  -- 'A'..'F'; 'F' = "Reliability cannot be judged" (cold start)
                       CHECK (reliability IN ('A','B','C','D','E','F')),
  credibility          SMALLINT NOT NULL DEFAULT 6   -- 1..6; 6 = "Truth cannot be judged" (cold start)
                       CHECK (credibility BETWEEN 1 AND 6),
  reputation_meta      JSONB NOT NULL DEFAULT '{}'::jsonb  -- reserved for full Admiralty / AJP-2.1 (per-asset-class vectors, Phase 2)
);

-- PII separation (handle is sensitive; source_id is opaque)
CREATE TABLE source_identity (
  source_id            TEXT PRIMARY KEY REFERENCES sources(source_id),
  tg_user_id           BIGINT UNIQUE NOT NULL,
  tg_handle            TEXT,
  email                TEXT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Event-sourced reputation history (never overwrite)
CREATE TABLE source_reputation_history (
  history_id           BIGSERIAL PRIMARY KEY,
  source_id            TEXT NOT NULL REFERENCES sources(source_id),
  effective_at         TIMESTAMPTZ NOT NULL,
  -- Admiralty 2-axis
  reliability          CHAR(1) NOT NULL CHECK (reliability IN ('A','B','C','D','E','F')),
  credibility          SMALLINT NOT NULL CHECK (credibility BETWEEN 1 AND 6),
  reputation_meta      JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Auxiliary fields preserved for ML training set (§11.1.A)
  n_resolved_claims    INTEGER NOT NULL DEFAULT 0,
  market_impact        DOUBLE PRECISION NOT NULL DEFAULT 0,
  latency_p50_ms       INTEGER NULL,
  specialization       JSONB NOT NULL DEFAULT '{}'::jsonb,
  trigger_event        TEXT NOT NULL,               -- e.g. 'outcome.resolved', 'admin.override'
  trigger_ref          TEXT NULL                    -- outcome_id, override_id, ...
);
CREATE INDEX idx_srh_source_time ON source_reputation_history(source_id, effective_at DESC);

-- ============================================================
-- Market layer
-- ============================================================

CREATE TABLE assets (
  asset_id             TEXT PRIMARY KEY,            -- 'asset:crypto:bitcoin' (derived from coingecko_id)
  coingecko_id         TEXT NOT NULL UNIQUE,        -- CoinGecko's canonical id; resolver's primary lookup key
  symbol               TEXT NOT NULL,               -- CoinGecko's `symbol` upper-cased ('BTC', 'ETH', …)
  name                 TEXT NOT NULL,               -- CoinGecko's `name` ('Bitcoin', 'Ethereum', …)
  kind                 TEXT NOT NULL,               -- 'crypto' at v0; 'equity' | … reserved
  entity_id            TEXT NULL,                   -- references entities (optional)
  metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Rows are materialised lazily by POST /assets/resolve during the
-- bot's /alpha flow (Q-027).
-- No INSERT seed, no scheduled sync.

CREATE TABLE asset_aliases (
  alias                TEXT PRIMARY KEY,            -- operator-supplied override only
  asset_id             TEXT NOT NULL REFERENCES assets(asset_id),
  source               TEXT NOT NULL,               -- 'admin' (operator override per Q-023 asset rename) | 'auto' (future)
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 'seed' source removed 2026-05-26: aliases are no longer pre-loaded;
-- the bot's search step uses CoinGecko's own fuzzy search instead.
-- This table is reserved for operator overrides on asset mutations
-- (Q-023 — e.g. mapping XYZ → XYZ2 before t_resolve).

CREATE TABLE entities (
  entity_id            TEXT PRIMARY KEY,
  name                 TEXT NOT NULL,
  sector               TEXT NULL,
  metadata             JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ============================================================
-- Claim graph
-- ============================================================

CREATE TABLE hypotheses (
  hypothesis_id        TEXT PRIMARY KEY,
  asset_id             TEXT NOT NULL REFERENCES assets(asset_id),
  direction            TEXT NOT NULL,               -- 'long' | 'short'
  horizon              TEXT NOT NULL,               -- '1h' | ... | '90d'
  bucketing_rule       TEXT NOT NULL,               -- 'asset_direction_horizon' at v0
  thesis_centroid      vector(384) NULL,            -- populated Day 60+
  belief_score         DOUBLE PRECISION NOT NULL DEFAULT 0,
  market_impact        DOUBLE PRECISION NOT NULL DEFAULT 0,
  attribution_weights  JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE claims (
  claim_id             TEXT PRIMARY KEY,
  source_id            TEXT NOT NULL REFERENCES sources(source_id),
  asset_id             TEXT NOT NULL REFERENCES assets(asset_id),
  hypothesis_id        TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
  direction            TEXT NOT NULL CHECK (direction IN ('long','short')),
  horizon              TEXT NOT NULL CHECK (horizon IN ('1h','4h','24h','3d','7d','14d','30d','90d')),
  conviction           TEXT NOT NULL CHECK (conviction IN ('weak','medium','strong','very_strong')),
  thesis_text          TEXT NULL,
  thesis_embedding     vector(384) NULL,            -- populated Day 60+
  submitted_at         TIMESTAMPTZ NOT NULL,
  -- Q-028: freeform-LLM ingest path.
  -- `parsed_via` discriminates LLM vs. button-form claims for analytics.
  -- `extracted_fields` is the user-attested nuance the v0 verifier doesn't
  -- score (target prices, calendar gates, named entities); Phase 2 verifiers
  -- may extend resolution against it without a schema migration. Frozen at
  -- claim commit per §15 invariant 17.
  -- `parse_metadata` is the audit trail: {model, model_version, prompt_hash,
  --   raw_user_turns, confidences, extracted_at}.
  --   `raw_user_turns[0]` is the immutable original freeform message even
  --   if the user later edits the thesis on preview.
  parsed_via           TEXT NOT NULL CHECK (parsed_via IN ('freeform_llm', 'form')),
  extracted_fields     JSONB NOT NULL DEFAULT '{}'::jsonb,
  parse_metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- No `ingest_hash` at v0; cryptographic / DLT anchoring lands in Phase 2.
  edits                JSONB NOT NULL DEFAULT '[]'::jsonb,
  soft_deleted_at      TIMESTAMPTZ NULL
);
CREATE INDEX idx_claims_source_time ON claims(source_id, submitted_at DESC);
CREATE INDEX idx_claims_hypothesis ON claims(hypothesis_id);
CREATE INDEX idx_claims_asset_time ON claims(asset_id, submitted_at DESC);

CREATE TABLE events (
  event_id             TEXT PRIMARY KEY,
  asset_id             TEXT NOT NULL REFERENCES assets(asset_id),
  resolution_type      TEXT NOT NULL,               -- 'auto' | 'oracle' | 'manual'
  resolution_source    TEXT NOT NULL,
  resolution_rule      TEXT NOT NULL,
  t0                   TIMESTAMPTZ NOT NULL,
  t_resolve            TIMESTAMPTZ NOT NULL,
  status               TEXT NOT NULL DEFAULT 'pending',
                       -- 'pending' | 'resolved' | 'resolution_failed' | 'unresolvable' | 'rejected_asset_mutation'
                       -- 'rejected_asset_mutation' = asset delisted / split / renamed without alias-chain mapping
                       --   between t0 and t_resolve. No Outcome row is written
                       --   for events in this state; score aggregator filters on status='resolved'.
  asset_state_at_t0    JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (t_resolve > t0)
);
CREATE INDEX idx_events_pending_t_resolve ON events(t_resolve) WHERE status = 'pending';

-- Link table (1 Claim → 1 Event at v0; future-proofed as N:1)
CREATE TABLE claim_events (
  claim_id             TEXT PRIMARY KEY REFERENCES claims(claim_id),
  event_id             TEXT NOT NULL REFERENCES events(event_id)
);

CREATE TABLE outcomes (
  outcome_id           TEXT PRIMARY KEY,
  event_id             TEXT NOT NULL REFERENCES events(event_id),
  resolved_at          TIMESTAMPTZ NOT NULL,
  observed_return      DOUBLE PRECISION NOT NULL,
  price_at_t0          DOUBLE PRECISION NOT NULL,
  price_at_t_resolve   DOUBLE PRECISION NOT NULL,
  resolution_price_t0_ref        TEXT NOT NULL,
  resolution_price_resolve_ref   TEXT NOT NULL,
  -- ML reservations (§11)
  intermediate_returns JSONB NOT NULL DEFAULT '{}'::jsonb,
                       -- {"1h": 0.004, "4h": 0.011, "24h": 0.018}
  max_favorable_excursion  DOUBLE PRECISION NULL,
  max_adverse_excursion    DOUBLE PRECISION NULL,
  benchmark_return         DOUBLE PRECISION NULL,
  -- Counterfactual branches (§2.7)
  pnl_if_followed_usd  DOUBLE PRECISION NOT NULL,
  pnl_if_ignored_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,
  -- regret = followed - ignored, derived; not stored as canonical truth
  followed_in_practice BOOLEAN NOT NULL DEFAULT FALSE,
  supersedes_outcome_id TEXT NULL REFERENCES outcomes(outcome_id)
);
CREATE INDEX idx_outcomes_event ON outcomes(event_id);

-- ============================================================
-- Attribution & independence
-- ============================================================

CREATE TABLE attribution_ledger (
  entry_id             TEXT PRIMARY KEY,
  hypothesis_id        TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
  source_id            TEXT NOT NULL REFERENCES sources(source_id),
  claim_id             TEXT NOT NULL REFERENCES claims(claim_id),
  role                 TEXT NOT NULL CHECK (role IN ('originator','amplifier','corroborator')),
  timing_advantage_ms  BIGINT NOT NULL DEFAULT 0,
  timing_advantage_anchor TEXT NOT NULL DEFAULT 'first_claim_in_hypothesis',
                       -- 'first_claim_in_hypothesis' | 'price_move_threshold' | 'external_news' | 'manual'
  downstream_pnl_attributed_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_attr_hypothesis ON attribution_ledger(hypothesis_id);
CREATE INDEX idx_attr_source ON attribution_ledger(source_id);

-- Empty at v0; reserved for independence detection (§11.1.C)
CREATE TABLE claim_similarity_edges (
  claim_a_id           TEXT NOT NULL REFERENCES claims(claim_id),
  claim_b_id           TEXT NOT NULL REFERENCES claims(claim_id),
  cosine_similarity    DOUBLE PRECISION NOT NULL,
  method               TEXT NOT NULL,               -- 'exact_match' | 'embedding' | 'manual'
  detected_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (claim_a_id, claim_b_id),
  CHECK (claim_a_id < claim_b_id)                   -- canonical ordering
);

-- ============================================================
-- Admin: invites & overrides
-- ============================================================

CREATE TABLE invites (
  invite_id            TEXT PRIMARY KEY,
  code                 TEXT UNIQUE NOT NULL,
  created_by_source_id TEXT NOT NULL REFERENCES sources(source_id),
                       -- doubles as referral attribution edge (§12.6, controlled growth)
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at           TIMESTAMPTZ NOT NULL,
  redeemed_by_source_id TEXT NULL REFERENCES sources(source_id),
  redeemed_at          TIMESTAMPTZ NULL,
  revoked_at           TIMESTAMPTZ NULL,
  revoked_reason       TEXT NULL
);
CREATE INDEX idx_invites_creator ON invites(created_by_source_id);

-- Waitlist for arrivals without an invite code (§12.6)
CREATE TABLE waitlist (
  waitlist_id          TEXT PRIMARY KEY,
  tg_user_id           BIGINT UNIQUE NOT NULL,
  tg_handle            TEXT,
  requested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  status               TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','invited','rejected','expired')),
  invite_id            TEXT NULL REFERENCES invites(invite_id),
  notes                TEXT NULL
);
CREATE INDEX idx_waitlist_status ON waitlist(status, requested_at);

CREATE TABLE override_log (
  override_id          TEXT PRIMARY KEY,
  operator_source_id   TEXT NOT NULL REFERENCES sources(source_id),
  target_table         TEXT NOT NULL,
  target_id            TEXT NOT NULL,
  field                TEXT NOT NULL,
  old_value            JSONB NULL,
  new_value            JSONB NOT NULL,
  reason               TEXT NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_override_target ON override_log(target_table, target_id);
```

### Notes

- All monetary columns use `DOUBLE PRECISION` for v0 simplicity; switch to `NUMERIC(20,8)` if currency-precision drift becomes an issue.
- `vector(384)` is sized for common open-source embedding models (e.g. `sentence-transformers/all-MiniLM-L6-v2`). Adjustable when Day 60 picks a model.
- `claim_events` is a separate table to future-proof for the N-claim-to-1-event case Day 60 may need; at v0 every claim has exactly one event.
- The `outcomes.supersedes_outcome_id` pointer is how re-resolution works under §15 invariant "Outcome written exactly once" — you write a new row pointing at the superseded one; the leaderboard query takes the latest.

---

## 14. Lazy asset resolution

There is **no asset seed list and no scheduled catalog sync**. The
`assets` table starts empty on a fresh DB and grows on demand as users
make claims. CoinGecko is the source of truth for asset identity,
symbol, and name; the local table is a cache keyed by `coingecko_id`.

### Flow

```
User             Bot                       API                      CoinGecko        DB
 │                │                         │                         │              │
 │  /alpha       │                         │                         │              │
 ├──────────────>│                         │                         │              │
 │               │  "Search asset:" prompt │                         │              │
 │<──────────────┤                         │                         │              │
 │  "render"     │                         │                         │              │
 ├──────────────>│                         │                         │              │
 │               │  GET /assets/search?q=render                      │              │
 │               ├────────────────────────>│                         │              │
 │               │                         │  GET /api/v3/search?query=render        │
 │               │                         ├────────────────────────>│              │
 │               │                         │  [{id:"render-token", symbol:"RNDR",   │
 │               │                         │   name:"Render", market_cap_rank:64},  │
 │               │                         │   {id:"marlin", …}, …]                 │
 │               │                         │<────────────────────────┤              │
 │               │  candidates (top 5)     │                         │              │
 │               │<────────────────────────┤                         │              │
 │  buttons      │                         │                         │              │
 │<──────────────┤                         │                         │              │
 │  picks RNDR   │                         │                         │              │
 ├──────────────>│                         │                         │              │
 │               │  POST /assets/resolve {coingecko_id:"render-token"}               │
 │               ├────────────────────────>│                         │              │
 │               │                         │  SELECT asset_id FROM assets WHERE coingecko_id = $1
 │               │                         ├──────────────────────────────────────>│
 │               │                         │                         │  (miss)      │
 │               │                         │  INSERT INTO assets (asset_id, coingecko_id, symbol, name, kind)
 │               │                         │  VALUES (…) ON CONFLICT (coingecko_id) DO NOTHING RETURNING …
 │               │                         ├──────────────────────────────────────>│
 │               │  {asset_id, symbol, name}                                       │
 │               │<────────────────────────┤                         │              │
 │               │  advances to step 2     │                         │              │
 │<──────────────┤                         │                         │              │
```

### Endpoints (Sprint 1 — `apps/api/merlin_api/routes/assets.py`)

- `GET /assets/search?q=<text>` — proxies `GET /api/v3/search?query=<text>` on CoinGecko, filters to `coins` only, returns up to 5 candidates ordered by `market_cap_rank` (NULL last). Response shape: `[{coingecko_id, symbol, name, market_cap_rank}]`.
- `POST /assets/resolve` — body `{coingecko_id, symbol_hint?, name_hint?}`. Logic:
  1. `SELECT asset_id FROM assets WHERE coingecko_id = $1` — fast path.
  2. On miss: fetch `GET /api/v3/coins/{id}?localization=false&tickers=false&market_data=false&community_data=false&developer_data=false` to confirm `coingecko_id` exists and get canonical `symbol` + `name` (defence against a client passing junk).
  3. `INSERT INTO assets (asset_id, coingecko_id, symbol, name, kind) VALUES ('asset:crypto:'||$2, $1, $3, $4, 'crypto') ON CONFLICT (coingecko_id) DO NOTHING RETURNING …` — `asset_id` is derived deterministically from `coingecko_id` so two concurrent first-claims for RNDR materialise the same row.
  4. Return `{asset_id, coingecko_id, symbol, name}`.

### Why no seed and no sync

- **Seed:** the `assets` table reflects exactly the assets users care about; we never carry rows we don't need. New listings work the moment CoinGecko knows about them; ancient tokens don't bloat the picker.
- **Sync:** a scheduled CoinGecko top-100 upsert would create rows users never reference, and would do nothing for the long-tail tokens claims actually want. The lazy path covers both ends.
- **Operator catalog management:** removed. The only operator-facing asset surface that survives is `asset_aliases` for the Q-023 rename path (e.g. mapping `XYZ → XYZ2` before `t_resolve`).

### Test determinism

Production has no seed; the test suite has fixtures. Integration tests inject a tiny known catalog (BTC + ETH + a sentinel `TEST-COIN` for negative paths) via a pytest fixture in `apps/api/tests/conftest.py` — never via a migration. A future-Sprint contract test asserts the resolver fails closed on a `coingecko_id` CoinGecko doesn't know.

### Failure modes

| Condition | Handling |
| --- | --- |
| User query returns zero candidates | Bot replies "no matches — try a ticker or full name". No DB write. |
| User picks a candidate but `POST /assets/resolve` confirmation call to CoinGecko 5xxs | Bot replies "couldn't reach market data — try again in a moment". No DB write. Retried per MVP §12.1. |
| Two users resolve the same `coingecko_id` simultaneously | `ON CONFLICT (coingecko_id) DO NOTHING` collapses both into one row; both clients re-SELECT and get the same `asset_id`. |
| Asset materialised, then CoinGecko later renames or delists it | Outcome resolution surfaces it via the Q-023 path (§7 edge cases). The `assets` row stays — `asset_id` is immutable (MVP §15 invariant 13). |

---

## 15. Implicit invariants

Properties the system relies on but that aren't stated as a feature anywhere. Each one is testable; each one is a regression risk if violated.

1. **`t_resolve > submitted_at`** for every Event. Enforced by DB `CHECK` constraint and by the ingest service.
2. **`pnl_if_ignored_usd = 0`** for every v0 Outcome. The "ignored" baseline is flat; pinned by §2.7. If it ever changes, the migration must backfill, not silently mutate the default.
3. **`regret_usd = pnl_if_followed_usd - pnl_if_ignored_usd`** is **derived**, never stored as canonical truth. The leaderboard query computes it; if the sizing rule changes, historical `pnl_if_followed_usd` stays unchanged and the leaderboard can be recomputed deterministically.
4. **Horizons are exact seconds.** `24h = 86400 s`, `30d = 2592000 s`, `90d = 7776000 s`. No business-day math, no calendar math, no DST.
5. **All timestamps are UTC** in storage and computation. UTC display is the bot's responsibility (§12.10).
6. **Outcome is written exactly once per Event.** Re-resolution writes a *new* Outcome row with `supersedes_outcome_id` pointing at the old one; the leaderboard reads only the latest non-superseded Outcome.
7. **Claims are append-only.** `claims.edits` is a JSONB array; the original payload at ingest is the canonical record. No `UPDATE` statements on `claims` outside the `edits` field and the `soft_deleted_at` flag.
8. **Reputation history is append-only.** Never `UPDATE source_reputation_history`. The "current reputation" is `SELECT … ORDER BY effective_at DESC LIMIT 1`.
9. **`source_id` is opaque.** All PII (TG handle, email) lives in `source_identity` behind access control. The training-set export pipeline (future) never joins to `source_identity`.
10. **Append-only audit log is the canonical record.** A Claim's original payload is whatever the row holds at insert time; subsequent edits append to `claims.edits` JSONB with timestamps. Cryptographic / DLT-backed anchoring is Phase 2+. When DLT anchoring lands, it writes proofs *over* the existing audit trail without a backfill or schema migration.
11. **`originator` role is unique per Hypothesis.** Tie-breaker per §12.3. Trivially satisfied at v0 (1:1 Hypothesis ↔ Claim, Q-007); enforced by a partial unique index that becomes load-bearing in Phase 2 when bucketing arrives.
12. **No hard DELETE** on any table that participates in the closed loop (`sources`, `claims`, `events`, `outcomes`, `attribution_ledger`, `hypotheses`). Soft-delete only. Hard DELETE is reserved for `invites` (unredeemed/revoked) and nothing else.
13. **`asset_id` is immutable.** Once issued, an asset_id is never reassigned. Rebrands / renames use a new asset_id and the `asset_aliases` table carries the old symbol forward.
14. **The leaderboard query is deterministic** given the underlying tables — no randomness, no `now()`-dependence except for the rolling-window cutoff. Two operators querying within the same second see identical rows.
15. **Scheduling is Redis-only.** No `pg_cron`, no operator-side cron. Resolution is `queue.enqueue_at(t_resolve, …)`; reputation recompute is reactive (enqueued by the resolver on outcome write); invite expiry is checked at redemption. `apps/worker/merlin_worker/schedules/` hosts the worker's RQ `CronScheduler` (still Redis-driven) with two recurring jobs: the resolution-reconciliation sweep (ADR (v) / Q-030 — the RQ-only `enqueue_at` primitive can lose a job to Redis eviction/restart, and the durable `claims` table is the source of truth it reconciles against) and the first-call engagement nudge (ADR (w) / Q-031 — limits tracked in the durable `engagement_nudges` table). Any *further* periodic job still requires a new ADR. Q-029 / Q-030 / Q-031.
16. **The bot voice is the product** (Q-028). Internal terminology (`source`, `claim`, `event`, `outcome`, `hypothesis`, `regret`, `simulated`, `belief`, `canonical form`), version markers (`v0`, `MVP`, `Phase 2`), enum literals (`pending`, `freeform_llm`), raw ISO timestamps, and limitation hedges (`not scored at v0`, `kept for the record`) **never appear in user-facing strings**. Operator/admin commands are exempt. Enforced by code review + the `make docs-check` grep gate. See `PLAN.md` §6 ("Bot voice") for the full rules.
17. **`extracted_fields` is frozen at claim commit.** The `claims.extracted_fields` JSONB captures user-attested intent the v0 verifier doesn't score (target prices, calendar gates, named entities, rumour flags). Phase 2 verifiers may *read* it to extend resolution but **must never mutate it**. Re-resolution writes a new `outcomes` row via the supersedes chain (invariant 6) — the original claim row remains pristine. Enforced at the repository layer (no UPDATE path for `extracted_fields`) and via a Postgres column-level trigger introduced alongside the Q-028 migration. Q-028.
