# Merlin

**A Telegram bot that paper-trades crypto alpha calls and ranks the people who make them.**

Post a call like *"ETH long, two weeks, strong conviction"*. Merlin opens a virtual position at the price at that moment, closes it when the horizon ends, and scores you on how much following your call would have made or lost. Everyone ends up on a public leaderboard built from verified, timestamped, append-only records: no edited tweets, no deleted bad calls.

[![CI](https://github.com/asmodeoux/merlin/actions/workflows/ci.yml/badge.svg)](https://github.com/asmodeoux/merlin/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

<!-- Screenshots: add docs/assets/alpha.png, board.png, me.png and uncomment.
<p align="center">
  <img src="docs/assets/alpha.png" width="260" alt="/alpha freeform call">
  <img src="docs/assets/board.png" width="260" alt="/board leaderboard">
  <img src="docs/assets/me.png" width="260" alt="/me stats">
</p>
-->

> **Status:** built as a startup MVP in 2026, ran with a small invite-only group of testers, and is no longer maintained. Published as a reference for the architecture and the scoring model.

## How it works

```
 "SOL long 3d, high conviction"
            │
            ▼
   ┌──────────────────┐   LLM parses the sentence into a draft
   │ /alpha (Telegram)│   (asset, direction, horizon, conviction)
   └────────┬─────────┘   user confirms or edits a field
            ▼
   Claim stored (append-only) ── price at t0 from CoinGecko
            │
            ▼  RQ enqueue_at(t_resolve)
   Worker resolves at horizon ── price at t_resolve
            │
            ▼
   Outcome: pnl_if_followed, pnl_if_ignored  →  regret
            │
            ▼
   Reputation recomputed → leaderboard → DM to the caller
```

**Scoring.** Every call is scored with *two-sided counterfactual regret*: `regret = pnl_if_followed − pnl_if_ignored`. Conviction sets the virtual position size ($1 / $3 / $5 / $10), and the "ignored" baseline is flat. Because the score is counterfactual, every source can be ranked from its first call, without anyone committing capital. Source reputation uses the Admiralty 2-axis scale (reliability × credibility).

## Bot commands

| Command | What it does |
| --- | --- |
| `/start` | Onboard with an invite code and pick a display name |
| `/alpha` | Log a call — a plain sentence, or a guided button flow as fallback |
| `/board` | Leaderboard, sliceable by horizon (day traders ≤ 24h vs. swing) |
| `/me` | Your open calls, hit rate and PnL |
| `/history` | Resolved calls, newest first |
| `/tickers` | Your stats per asset |
| `/invite` · `/revoke` | Mint and revoke invite codes (the invite chain doubles as sybil resistance) |

## Architecture

Three deployable services and three shared packages in a Python monorepo, with hexagonal layering:

```
apps/
  api/      FastAPI — the only service that talks to the database
  bot/      python-telegram-bot v21 — a thin client of the API
  worker/   RQ jobs — resolution, reputation, durable sweep, nudges
packages/python/
  merlin_core/            domain + use cases + ports (stdlib-only domain)
  merlin_infrastructure/  SQLAlchemy repos, Alembic, CoinGecko, OpenRouter, Telegram
  merlin_client/          typed httpx SDK the bot uses to call the API
```

```
Telegram ⇄ bot ──HTTP──▶ api ──▶ Postgres (pgvector)
                          │
                          └──▶ Redis / RQ ──▶ worker ──▶ CoinGecko, Telegram DMs
```

Full details in [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md), including the ADR log and sequence diagrams.

## Engineering highlights

- **Layer rules enforced in CI.** `import-linter` contracts stop the bot from importing infrastructure, keep the domain stdlib-only, and so on. Breaking a boundary fails the build.
- **Append-only ledger.** Claims, outcomes, reputation history and overrides are never updated. Corrections insert a new row that supersedes the old one, so every leaderboard state can be replayed. Regret is derived, never stored.
- **Contract-first API.** Every route has a Pydantic contract in `merlin_core.contracts`, a typed client method, and a committed OpenAPI snapshot. CI fails if the snapshot drifts.
- **LLM as UX, not a dependency.** Freeform `/alpha` is one schema-constrained LLM call (≈ $0.0003). On failure, or once the monthly budget cap is hit, the bot falls back to the button flow.
- **Self-healing scheduling.** Resolution jobs live in Redis, but Postgres is the source of truth: a cron sweep re-fires any bet whose job was evicted. This was added after an incident where jobs silently disappeared ([ADR v / Q-030](./docs/DECISIONS.md)).
- **Preflight tests** for config drift between Compose, Dockerfiles, `.env.example`, Fly configs and Settings classes. Each one was added after a deploy-time bug that unit tests missed (see [`AGENTS.md`](./AGENTS.md) §5.1).
- **Quality gates:** `mypy --strict`, ruff, per-package coverage floors, ~880 tests, gitleaks, pip-audit, and Dependabot with cooldowns.
- **Privacy:** raw Telegram handles are never logged; sources are identified by an opaque hash.

## Tech stack

Python 3.12 · FastAPI · python-telegram-bot 21 · SQLAlchemy 2 (async) + asyncpg · Alembic · PostgreSQL 16 + pgvector · Redis + RQ · Pydantic 2 · httpx · OpenRouter · CoinGecko · Docker Compose · Fly.io · GitHub Actions · uv

## Running locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp apps/api/.env.example apps/api/.env       # then fill in the values
cp apps/bot/.env.example apps/bot/.env       # needs a bot token from @BotFather
cp apps/worker/.env.example apps/worker/.env

make install-frozen   # uv sync --frozen --all-extras
make dev-up           # postgres + redis + api/bot/worker with hot reload + debugpy
make dev-verify       # checks the stack is healthy
make ci               # ruff, mypy, import-linter, pytest, coverage, docs-check
```

The API serves at `http://localhost:8000` (`/docs` for Swagger). Deployment to Fly.io is documented in [`deploy/fly/README.md`](./deploy/fly/README.md).

## Docs

| Document | Contents |
| --- | --- |
| [`docs/MVP_SPEC.md`](./docs/MVP_SPEC.md) | Product spec, data model, full Postgres DDL, invariants, ML-readiness review |
| [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) | Runtime topology, layer model, ADRs |
| [`docs/DECISIONS.md`](./docs/DECISIONS.md) | Product and architecture decision log (`Q-NNN`) |
| [`docs/RUNBOOK.md`](./docs/RUNBOOK.md) | Operational procedures |
| [`AGENTS.md`](./AGENTS.md) | Contributor and AI-agent guide: layer rules, the bot ↔ API workflow, what not to do |

## Acknowledgements

Allen Day mentored me throughout this project. The closed-loop idea at the heart of Merlin, scoring every call by its regret, came out of our conversations. Rest in peace, Allen.

## License

[MIT](./LICENSE)
