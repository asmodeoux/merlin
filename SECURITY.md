# Security policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue. Email
`security@merlin.example` with:

- A short description of the issue.
- The version (commit SHA or release tag) you found it in.
- Reproduction steps or a proof-of-concept.

We aim to acknowledge reports within **48 hours** and to ship a fix or
mitigation within **14 days** for critical / high-severity issues.

PGP key (placeholder — replace before public launch):

```
-----BEGIN PGP PUBLIC KEY BLOCK-----
(replace before public launch)
-----END PGP PUBLIC KEY BLOCK-----
```

GitHub's **Private Vulnerability Reporting** is enabled on this repo as
an alternative reporting channel (see
[`docs/REPO_SETTINGS.md`](docs/REPO_SETTINGS.md)).

## Supported versions

`main` is the supported branch. We do not backport fixes to historical
tags during v0 / Phase 1.

## v0 security baseline

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §13 for the full
threat model and rationale. Highlights:

- **Dependency surface** — `uv.lock` committed; `dependabot.yml` with a
  7-day cooldown on default updates (30d major / 7d minor / 3d patch).
- **Scanners** — `gitleaks` (pre-commit + CI), `pip-audit` (CI + daily
  cron), `actions/dependency-review-action` (PR diff, blocks moderate+).
- **Secrets** — GitHub Secret Scanning + Push Protection enabled.
  Service tokens via `fly secrets set`; never committed. The four
  secrets the Sprint-1 stack actually consumes:

  | Var | Where it comes from | Notes |
  |---|---|---|
  | `TELEGRAM_BOT_TOKEN` | BotFather | Worker also reads this for outbound resolution notifications (direct Bot API call). |
  | `OPENROUTER_API_KEY` | openrouter.ai | Empty disables the freeform `/alpha` path; the bot falls back to the button form silently. |
  | `COINGECKO_API_KEY` | coingecko.com (Demo tier free) | Same env var name carries forward to paid tiers — the base URL discriminates. |
  | `MERLIN_API_INTERNAL_TOKEN` | `openssl rand -hex 32` | HMAC bearer for bot↔API and worker↔API. Rotate via `fly secrets set` on every app together. |

  `LLM_MONTHLY_USD_BUDGET` is a cost-safety knob, not a secret: when
  the running spend exceeds it, the API short-circuits freeform drafts
  to the button form. Set conservatively (≤$3) before external testers.
- **CI permissions** — top-of-file `permissions: contents: read` on
  every workflow; widened per-job only where strictly required.
- **AI-generated code** — disclosed in the PR template; CI gates
  (ruff + mypy + import-linter + tests) apply equally to AI-written
  code.

## Reproducing repo settings

See [`docs/REPO_SETTINGS.md`](docs/REPO_SETTINGS.md) and the
`scripts/bootstrap_github_settings.sh` script.
