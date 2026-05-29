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


## Monorepo layout

Merlin is a **polyglot monorepo**. Today everything is Python; a React frontend lands in Phase 2 against the same flat API surface (no URL versioning at v0 — the OpenAPI `info.version` is the canonical version marker).

```
apps/        api/  bot/  worker/   ← deployable services (one Dockerfile each)
packages/    python/                ← shared Python libs (core, infrastructure, client)
deploy/      docker-compose dev + test, custom postgres-dev (pgvector)
scripts/     env_check.py, bootstrap_github_settings.sh
docs/        ARCHITECTURE, MVP_SPEC, RUNBOOK, REPO_SETTINGS, openapi.json
```

Key cross-links:

- **[`AGENTS.md`](./AGENTS.md)** — operational guide for AI agents and human contributors. Layer rules, the 5-step bot↔API ritual, what NOT to do.
- **[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)** — repo layout, hexagonal layers, ADRs (a–q), multi-frontend strategy, sequence diagrams.
- **[`docs/openapi.json`](./docs/openapi.json)** — committed OpenAPI 3.1 snapshot. Snapshot test catches drift; `make openapi` regenerates.
- **[`docs/RUNBOOK.md`](./docs/RUNBOOK.md)** — operational playbook (stub; Sprint 4 fills pre-launch).
- **[`docs/REPO_SETTINGS.md`](./docs/REPO_SETTINGS.md)** — GitHub bootstrap checklist + `scripts/bootstrap_github_settings.sh`.
- **[`SECURITY.md`](./SECURITY.md)** — vuln disclosure + v0 security baseline.

## Dev loop

```bash
make install-frozen   # uv sync --frozen --all-extras
make dev-up           # postgres + redis + api/bot/worker, hot reload + debugpy 5678/5679/5680
make dev-verify       # one-button "is the stack alive" — boots compose, polls /health + /ready,
                      # greps bot+worker startup lines, prints clickable URLs (≤ 30s on a fresh checkout)
make ci               # full local CI parity: ruff, mypy, import-linter, pytest, coverage, docs-check
make docker-test      # prod images + integration suite (same images Fly ships)
```

VS Code: `.vscode/launch.json` ships "Attach to API/Bot/Worker (Docker debugpy)" entries grouped under **2_apps_docker** and one-shot test runners under **3_tests**.

## Status

Scaffolded; Sprint 1 fills business code. `make ci` green after the first `uv sync` regenerates `uv.lock` + `docs/openapi.json`.
