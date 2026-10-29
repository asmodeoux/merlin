# Security

This project is archived and not running anywhere. There is no live
deployment and no user data to protect, so there is no private
disclosure process. If you spot a security problem in the code, please
[open an issue](https://github.com/asmodeoux/merlin/issues).

## Security measures in the codebase

These were in place while the project was live and are still useful as
a reference. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §10
for the rationale.

- **Dependencies** — `uv.lock` is committed and checked in CI.
- **Scanners** — `gitleaks` (pre-commit + CI), `pip-audit` (CI), and
  `actions/dependency-review-action` on pull requests.
- **Secrets** — nothing is committed. Each app reads its secrets from
  environment variables (`fly secrets set` in production, `.env`
  locally):

  | Var | Where it comes from | Notes |
  |---|---|---|
  | `TELEGRAM_BOT_TOKEN` | BotFather | The worker also uses it to send resolution notifications. |
  | `OPENROUTER_API_KEY` | openrouter.ai | Leave empty to disable the freeform `/alpha` path; the bot falls back to the button flow. |
  | `COINGECKO_API_KEY` | coingecko.com (Demo tier is free) | The same variable works for paid tiers; the base URL picks the tier. |
  | `MERLIN_API_INTERNAL_TOKEN` | `openssl rand -hex 32` | Bearer token for bot → API and worker → API calls. Rotate on every app together. |

  `LLM_MONTHLY_USD_BUDGET` is a cost cap, not a secret: once spend
  passes it, the API stops making LLM calls and the bot uses the button
  flow.
- **CI permissions** — every workflow starts with
  `permissions: contents: read` and widens per job only where needed.
- **Privacy** — raw Telegram handles are never logged; sources are
  identified by an opaque hash (ARCHITECTURE §16 invariant 9).
