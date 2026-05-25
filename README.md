# Merlin

> **Merlin ranks crypto KOLs and traders by the regret of their alpha calls, paper-traded against live market data.** A Telegram bot ingests structured trade theses, virtually opens positions at the moment of the message, virtually closes them at the user's chosen horizon, and feeds the resulting regret back into a public leaderboard.

> **The leaderboard is the product.** Crypto KOL trading is already an attention-driven competitive surface — people *post* their calls and try to look right after the fact. Merlin honors that as **esports for crypto traders**: a public ranking with honest scoring, replayable claims, and a controlled-growth referral mechanic. The closed-loop math is the rulebook.

**By Day 30:** TG bot live, ≥ 10 invited sources, ≥ 100 resolved claims, leaderboard sanity check passes.
**By Day 60:** belief graph online (semantic hypothesis clustering, Twitter as a second source kind, public landing page).
**By Day 90:** Pro Dashboard, Polymarket signal, anti-sybil defenses, ≥ 1 paid pilot in writing.

Concrete user-flow example in [`docs/MVP_SPEC.md`](./docs/MVP_SPEC.md) §0.

## Quick orientation

If you're new to this repo, read the docs in this order:

| # | Document | What it is |
| --- | --- | --- |
| 2 | [`docs/MVP_SPEC.md`](./docs/MVP_SPEC.md) | **The buildable Day-30 spec.** Canonical schema, full Postgres DDL, operational notes, ML-readiness assessment, implicit invariants. The single source of truth for what gets coded. |

## Authorship & decision rules


## Status

Pre-code. All documents in this repo are working drafts. **DECISIONS Critical pass complete 2026-05-25 — Sprint 1 is unblocked.** First commit of source code lands on **Day 1 of Sprint 1**.
